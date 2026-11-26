# -*- coding: utf-8 -*-
"""
xhs-heat · reference_lane.py
对照车道（副）：只要「量」不要「深」，回答双热/独热/倒挂

实测结论（2026-11-26）：
  - RSSHub 股吧路由在 4 个公共实例全 404（主仓已删）→ 不再尝试
  - RSSHub /xueqiu/hots 在 hub.slarker.me 稳定 200，返回 10 条热帖正文 → 做资产提及计数
  - 雪球直连 API 命中阿里云 WAF（_waf_ 挑战页）→ 不用
  - 东财人气榜 emappdata 可用（与 push2 行情是不同风控面）→ 做散户人气旁证

纪律：
  - 参考车道失败不阻断主车道（v3 第 4.1 节降级 2）
  - 匹配总量低于 gate.min_matched_mentions 时，不判定双热/独热/倒挂，标「参照样本不足」
"""

import html
import json
import re

from common import (decode, http_get, http_post_json, log, norm_text,
                    truncate)

TAG_DOUBLE = "double_hot"        # 双热：xhs 高 + 对照高
TAG_XHS_ONLY = "xhs_only_hot"    # 独热：xhs 高 + 对照冷
TAG_REF_ONLY = "reference_only_hot"  # 倒挂：对照热 + xhs 冷


# ---------------------------------------------------------------------------
# 雪球热帖（RSSHub 实例池）
# ---------------------------------------------------------------------------
def fetch_xueqiu_hots(cfg, dict_obj):
    lane = cfg["lanes"]["reference"]
    route = "/xueqiu/hots"
    for src in lane["sources"]:
        if src["id"] == "xueqiu_hots" and src.get("routes"):
            route = src["routes"][0]
            break

    errors = []
    for base in lane["rsshub_instances"]:
        url = base.rstrip("/") + route
        code, body = http_get(url, timeout=15, retries=0)
        if code != 200:
            errors.append("%s → HTTP %s" % (base, code))
            continue
        text = decode(body)
        if "_waf_" in text[:400]:
            errors.append("%s → WAF 挑战" % base)
            continue
        items = _parse_rss(text)
        if not items:
            errors.append("%s → 0 条" % base)
            continue
        log("  ✅ 雪球热帖：%s（%d 条）" % (base, len(items)))
        return {"ok": True, "instance": base, "items": items, "errors": errors}

    log("  ⚠️ 雪球热帖全部实例失败：" + "；".join(errors[:3]))
    return {"ok": False, "instance": None, "items": [], "errors": errors}


def _parse_rss(xml_text):
    items = []
    for raw in re.findall(r"<item>(.*?)</item>", xml_text, re.S):
        title = _tag(raw, "title")
        desc = _tag(raw, "description")
        link = _tag(raw, "link")
        creator = _tag(raw, "dc:creator") or _tag(raw, "author")
        body = _strip_html(desc) or title
        if not body:
            continue
        items.append({
            "title": _strip_html(title),
            "text": body[:1500],
            "creator": creator,
            "link": link,
        })
    return items


def _tag(raw, name):
    m = re.search(r"<%s>(.*?)</%s>" % (name, name), raw, re.S)
    if not m:
        return ""
    v = m.group(1).strip()
    v = re.sub(r"^<!\[CDATA\[(.*)\]\]>$", r"\1", v, flags=re.S)
    return html.unescape(v).strip()


def _strip_html(s):
    s = re.sub(r"<br\s*/?>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


# ---------------------------------------------------------------------------
# 东财人气榜（个股，旁证）
# ---------------------------------------------------------------------------
def fetch_em_popularity(cfg, limit=20):
    src = None
    for s in cfg["lanes"]["reference"]["sources"]:
        if s["id"] == "em_popularity":
            src = s
            break
    if not src or not src.get("enabled"):
        return {"ok": False, "items": [], "note": "未启用"}

    payload = dict(src.get("payload") or {})
    payload["pageSize"] = limit
    code, body = http_post_json(src["url"], payload, timeout=15,
                                headers={"Referer": "https://quote.eastmoney.com/"})
    if code != 200:
        log("  ⚠️ 东财人气榜 HTTP %s" % code)
        return {"ok": False, "items": [], "note": "HTTP %s" % code}
    try:
        data = json.loads(decode(body))
    except ValueError:
        return {"ok": False, "items": [], "note": "响应非 JSON"}
    rows = data.get("data") or []
    if not rows:
        return {"ok": False, "items": [], "note": "空列表"}

    codes = [str(r.get("sc") or "").lower() for r in rows if r.get("sc")]
    names = _quote_names(codes)
    items = []
    for r in rows:
        sc = str(r.get("sc") or "").lower()
        items.append({
            "rank": r.get("rk"),
            "code": sc.upper(),
            "name": names.get(sc, ""),
            "rank_change": r.get("hisRc"),
        })
    log("  ✅ 东财人气榜：%d 只（%s 等）" % (len(items), items[0]["name"] or items[0]["code"]))
    return {"ok": True, "items": items, "note": ""}


def fetch_ths_hot(cfg, limit=100):
    """同花顺热榜（A股 100 只）：自带名称、热度值、涨跌幅、概念标签、排名变化。"""
    src = None
    for s in cfg["lanes"]["reference"]["sources"]:
        if s["id"] == "ths_hot":
            src = s
            break
    url = (src or {}).get("url") or (
        "https://dq.10jqka.com.cn/fuyao/hot_list_data/out/hot_list/v1/stock"
        "?stock_type=a&type=hour&list_type=normal")
    code, body = http_get(url, timeout=15, retries=1,
                          headers={"Referer": "https://eq.10jqka.com.cn/"})
    if code != 200:
        log("  ⚠️ 同花顺热榜 HTTP %s" % code)
        return {"ok": False, "items": [], "note": "HTTP %s" % code}
    try:
        data = json.loads(decode(body))
    except ValueError:
        return {"ok": False, "items": [], "note": "响应非 JSON"}
    rows = ((data.get("data") or {}).get("stock_list") or [])[:limit]
    items = []
    for r in rows:
        tag = r.get("tag") or {}
        items.append({
            "code": str(r.get("code") or "").zfill(6),
            "name": r.get("name") or "",
            "rank": int(r.get("order") or 0),
            "heat": float(r.get("rate") or 0),
            "chg_pct": round(float(r.get("rise_and_fall") or 0), 2),
            "rank_change": r.get("hot_rank_chg"),
            "concepts": [c for c in (tag.get("concept_tag") or []) if c],
            "tag": tag.get("popularity_tag") or "",
        })
    log("  ✅ 同花顺热榜：%d 只（%s 等）" % (len(items), items[0]["name"] if items else "-"))
    return {"ok": True, "items": items, "note": ""}


def merge_popularity(em_items, ths_items, limit=30):
    """合并两榜：代码对齐，双榜共振单独标记。"""
    em = {}
    for x in em_items:
        c = str(x.get("code") or "").upper()
        c = c[2:] if len(c) > 6 and c[:2] in ("SZ", "SH", "BJ") else c
        if len(c) == 6:
            em[c] = x
    ths = {x["code"]: x for x in ths_items if x.get("code")}

    codes = set(em) | set(ths)
    need_names = [c for c in codes if not (ths.get(c) or {}).get("name")]
    names = _quote_names([("sh" if c.startswith(("6", "9")) else "sz") + c for c in need_names]) if need_names else {}

    rows = []
    for c in codes:
        e = em.get(c) or {}
        t = ths.get(c) or {}
        em_rank = e.get("rank")
        ths_rank = t.get("rank")
        ranks = [r for r in (em_rank, ths_rank) if r]
        if not ranks:
            continue
        best = min(ranks)
        rows.append({
            "code": c,
            "name": (t.get("name") or names.get(("sh" if c.startswith(("6", "9")) else "sz") + c) or ""),
            "em_rank": em_rank,
            "ths_rank": ths_rank,
            "best_rank": best,
            "both": bool(em_rank and ths_rank and em_rank <= 20 and ths_rank <= 20),
            "diverge": bool(em_rank and ths_rank and abs(em_rank - ths_rank) >= 20),
            "heat": t.get("heat"),
            "chg_pct": t.get("chg_pct"),
            "rank_change": t.get("rank_change"),
            "concepts": t.get("concepts") or [],
            "tag": t.get("tag") or "",
        })
    rows.sort(key=lambda x: (not x["both"], x["best_rank"]))
    return rows[:limit]


def build_concepts(stocks, top=12):
    """概念热度：两榜个股的概念标签按出现次数聚合。"""
    from collections import Counter
    cnt = Counter()
    members = {}
    for s in stocks:
        for c in s.get("concepts") or []:
            cnt[c] += 1
            members.setdefault(c, []).append(s["name"])
    out = []
    for name, n in cnt.most_common(top):
        out.append({"name": name, "count": n, "stocks": members[name][:4]})
    return out


def _quote_names(codes):
    """腾讯行情批量取名称（1 次请求 20 只，GBK）。"""
    if not codes:
        return {}
    url = "https://qt.gtimg.cn/q=" + ",".join(codes[:30])
    code, body = http_get(url, timeout=10, retries=0,
                          headers={"Referer": "https://gu.qq.com/"})
    if code != 200:
        return {}
    out = {}
    for line in decode(body).split(";"):
        m = re.search(r'v_(\w+)="([^"]*)"', line)
        if not m:
            continue
        fields = m.group(2).split("~")
        if len(fields) > 2 and fields[1]:
            out[m.group(1).lower()] = fields[1]
    return out


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def build(cfg, dict_obj):
    lane = cfg["lanes"]["reference"]
    if not lane.get("enabled"):
        return {"ok": False, "note": "对照车道未启用"}

    hots = fetch_xueqiu_hots(cfg, dict_obj)
    pop = fetch_em_popularity(cfg, limit=100)
    ths = fetch_ths_hot(cfg)
    all_stocks = merge_popularity(pop.get("items") or [], ths.get("items") or [], limit=100000)
    stocks = all_stocks[:30]
    concepts = build_concepts(all_stocks)

    mentions = {}
    posts = []
    for it in hots["items"]:
        r = dict_obj.match(it["text"])
        ids = [m["id"] for m in r["matched"] if dict_obj.is_investable(m["id"])]
        for aid in ids:
            mentions[aid] = mentions.get(aid, 0) + 1
        posts.append({"title": it["title"],
                      "text": it["text"],
                      "link": it["link"],
                      "creator": it.get("creator") or "",
                      "assets": [dict_obj.name_of(a) for a in ids]})

    matched_total = sum(mentions.values())
    gate_cfg = lane.get("gate") or {}
    min_mentions = int(gate_cfg.get("min_matched_mentions", 15))
    gate = {
        "passed": matched_total >= min_mentions,
        "matched_total": matched_total,
        "min_required": min_mentions,
        "note": "" if matched_total >= min_mentions
                else "参考车道仅匹配 %d 处提及（< %d），本期不做双热/独热判定" % (matched_total, min_mentions),
    }

    sources = []
    sources.append({"id": "xueqiu_hots", "name": "雪球热帖", "ok": hots["ok"],
                    "items": len(hots["items"]),
                    "note": hots["instance"] or "；".join(hots["errors"][:2])})
    sources.append({"id": "em_popularity", "name": "东财人气榜", "ok": pop["ok"],
                    "items": len(pop["items"]), "note": pop.get("note", "")})
    sources.append({"id": "ths_hot", "name": "同花顺热榜", "ok": ths["ok"],
                    "items": len(ths["items"]), "note": ths.get("note", "")})

    return {
        "ok": hots["ok"] or pop["ok"] or ths["ok"],
        "generated_at": None,
        "sources": sources,
        "gate": gate,
        "asset_mentions": {aid: {"name": dict_obj.name_of(aid), "count": c}
                           for aid, c in sorted(mentions.items(), key=lambda x: -x[1])},
        "xueqiu_posts": posts[:10],
        "em_popularity": pop["items"],
        "stocks": stocks,
        "concepts": concepts,
        "both_count": sum(1 for s in all_stocks if s["both"]),
        "diverge_count": sum(1 for s in all_stocks if s["diverge"]),
        "total_stocks": len(all_stocks),
    }


def tags_for(ranked, ref, cfg, top_n=10, ref_top_n=8):
    """给主榜资产打三态标记；参照样本不足时全部标 insufficient。"""
    gate = (ref or {}).get("gate") or {}
    out = {}
    if not gate.get("passed"):
        for r in ranked[:top_n]:
            out[r["id"]] = {"tag": "insufficient", "ref_count": None}
        return out

    min_asset = int(gate.get("min_asset_mentions", 2))
    mentions = sorted(((aid, v["count"]) for aid, v in (ref.get("asset_mentions") or {}).items()),
                      key=lambda x: -x[1])
    ref_rank = {aid: i + 1 for i, (aid, _c) in enumerate(mentions)}
    for r in ranked[:top_n]:
        rr = ref_rank.get(r["id"])
        cnt = (ref.get("asset_mentions") or {}).get(r["id"], {}).get("count", 0)
        if rr and cnt >= min_asset:
            tag = TAG_DOUBLE
        elif rr:
            tag = "xhs_hot_ref_weak"
        else:
            tag = TAG_XHS_ONLY
        out[r["id"]] = {"tag": tag, "ref_count": cnt, "ref_rank": rr}
    # 倒挂：对照车道前 3 但不在主榜 Top10
    top_ids = {r["id"] for r in ranked[:top_n]}
    for aid, cnt in mentions[:3]:
        if cnt < min_asset:
            continue
        if aid not in top_ids and aid not in out:
            out[aid] = {"tag": TAG_REF_ONLY,
                        "ref_count": (ref.get("asset_mentions") or {})[aid]["count"],
                        "ref_rank": ref_rank.get(aid), "name": (ref.get("asset_mentions") or {})[aid]["name"]}
    return out
