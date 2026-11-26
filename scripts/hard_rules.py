# -*- coding: utf-8 -*-
"""
xhs-heat · hard_rules.py
纯硬规则层：去重 / 营销识别 / 意图兜底分类（ai-radar 原则一）

四条营销规则（v2 第 07 节）：
  1. 多号同文：文本相似度 > 0.8 且发布间隔 < 6 小时
  2. 引流钩子词（加微/私信/领资料/进群/扫码…）
  3. 作者批次内商业化占比 > 30%（作者历史占比需跨日累积，此处用批次内近似 + 报告标注）
  4. 标题模板化（句式库匹配）

刻意不做的事：
  - 不做「多篇合并成一篇」这类改写，只标记，原文照留
  - 不因为营销就删条目：热度分剔除，占比照常展示（成色判断留给 polo）
"""

import difflib
from collections import defaultdict
from datetime import timedelta

from common import clean_text, norm_text


# ---------------------------------------------------------------------------
# 去重
# ---------------------------------------------------------------------------
def dedup(notes):
    """标题去重：保留互动量更高的一条，把关键词合并进去。

    跨日重复不剔除（趋势标记在热度层做），只处理同一批次内的重复。
    """
    best = {}
    order = []
    dropped = 0
    for n in notes:
        key = clean_text(n.get("title") or "")[:48]
        if not key:
            key = clean_text(n.get("desc") or "")[:48]
        if not key:
            continue
        if key not in best:
            best[key] = n
            order.append(key)
            continue
        dropped += 1
        cur = best[key]
        if engagement(n) > engagement(cur):
            n["keyword"] = _merge_kw(n.get("keyword"), cur.get("keyword"))
            best[key] = n
        else:
            cur["keyword"] = _merge_kw(cur.get("keyword"), n.get("keyword"))
    return [best[k] for k in order], dropped


def _merge_kw(a, b):
    a = a or ""
    b = b or ""
    if not a:
        return b
    if not b or b in a:
        return a
    return a + "," + b


def engagement(note):
    return (int(note.get("liked") or 0) * 1
            + int(note.get("collected") or 0) * 3
            + int(note.get("comment") or 0) * 2)


# ---------------------------------------------------------------------------
# 营销识别
# ---------------------------------------------------------------------------
def detect_marketing(notes, cfg):
    """就地打标记，返回统计 dict。"""
    mcfg = cfg["hard_rules"]["marketing"]
    funnel = [k.lower() for k in mcfg["funnel_keywords"]]
    templates = [k.lower() for k in mcfg["template_titles"]]
    dup_thr = float(mcfg["near_duplicate_threshold"])
    interval = float(mcfg["same_text_interval_hours"])
    author_thr = float(mcfg["author_commercial_ratio_threshold"])

    for n in notes:
        n["marketing"] = False
        n["marketing_reasons"] = []

    # 规则 2 / 4 / 广告声明：文本级
    for n in notes:
        text = norm_text((n.get("title") or "") + (n.get("desc") or ""))
        hits = [k for k in funnel if k in text]
        if hits:
            n["marketing"] = True
            n["marketing_reasons"].append("引流钩子:" + "/".join(hits[:3]))
        tpl = [k for k in templates if k in text]
        if tpl:
            n["marketing"] = True
            n["marketing_reasons"].append("模板标题:" + "/".join(tpl[:2]))
        if "合作" in text and ("广告" in text or "推广" in text):
            n["marketing"] = True
            n["marketing_reasons"].append("广告声明")

    # 规则 1：多号同文（先用哈希分桶，桶内再算相似度，避免 O(n²)）
    buckets = defaultdict(list)
    for n in notes:
        body = clean_text((n.get("title") or "") + (n.get("desc") or ""))
        if len(body) < 20:
            continue
        n["_body"] = body
        buckets[body[:40]].append(n)
        buckets[body[-40:]].append(n)

    checked = 0
    for _key, group in buckets.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a is b or a.get("author_hash") == b.get("author_hash"):
                    continue
                if checked > 4000:      # 极端情况下限流，避免跑爆
                    break
                checked += 1
                if not _within_hours(a, b, interval):
                    continue
                ba, bb = a.get("_body", ""), b.get("_body", "")
                if not ba or not bb:
                    continue
                sm = difflib.SequenceMatcher(None, ba[:300], bb[:300])
                if sm.quick_ratio() < dup_thr:
                    continue
                if sm.ratio() >= dup_thr:
                    for x in (a, b):
                        x["marketing"] = True
                        if "多号同文" not in "".join(x["marketing_reasons"]):
                            x["marketing_reasons"].append("多号同文")

    # 规则 3：作者批次内商业化占比（作者历史占比见 reports 的 author_burst 字段）
    by_author = defaultdict(list)
    for n in notes:
        if n.get("author_hash"):
            by_author[n["author_hash"]].append(n)
    burst_authors = []
    for author, group in by_author.items():
        if len(group) < 4:
            continue
        ratio = sum(1 for x in group if x["marketing"]) / float(len(group))
        if ratio >= author_thr:
            burst_authors.append({"author": author, "notes": len(group), "ratio": round(ratio, 2)})
            for x in group:
                x["marketing"] = True
                if "作者商业化占比" not in "".join(x["marketing_reasons"]):
                    x["marketing_reasons"].append("作者商业化占比")

    for n in notes:
        n.pop("_body", None)

    marked = sum(1 for n in notes if n["marketing"])
    return {
        "total": len(notes),
        "marketing": marked,
        "ratio": round(marked / float(len(notes)), 4) if notes else 0.0,
        "burst_authors": sorted(burst_authors, key=lambda x: -x["ratio"])[:5],
        "pairs_checked": checked,
    }


def _within_hours(a, b, hours):
    ta, tb = a.get("_ts"), b.get("_ts")
    if not ta or not tb:
        return True          # 时间缺失时不因时间否决，交给相似度判断
    return abs((ta - tb).total_seconds()) <= hours * 3600


# ---------------------------------------------------------------------------
# 意图兜底分类（LLM 不可用时的降级口径）
# ---------------------------------------------------------------------------
INTENTS = ["学", "买", "晒", "愁", "广"]


def classify_intent_rule(note, cfg):
    lexicon = cfg["hard_rules"]["intent_lexicon"]
    text = norm_text((note.get("title") or "") + (note.get("desc") or ""))
    if note.get("marketing"):
        return "广"
    scores = {}
    for label, words in lexicon.items():
        if label == "广":
            continue
        scores[label] = sum(1 for w in words if norm_text(w) in text)
    best = max(scores, key=lambda k: scores[k]) if scores else "学"
    if scores.get(best, 0) == 0:
        # 无任何信号：默认归"学"（科普/围观）而不是"广"，避免误伤
        return "学"
    return best


def intent_distribution(notes):
    total = len(notes)
    if total == 0:
        return {k: 0.0 for k in INTENTS}
    cnt = defaultdict(int)
    for n in notes:
        cnt[n.get("intent") or "学"] += 1
    return {k: round(cnt.get(k, 0) / float(total), 3) for k in INTENTS}
