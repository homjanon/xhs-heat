# -*- coding: utf-8 -*-
"""
xhs-heat · fetch_heat.py
主流程：读采集件 → 硬规则 → 词典 → LLM 长尾 → 热度 → 双留存 → 产物

用法：
  python scripts/fetch_heat.py                      # 处理 inbox 里今天的数据
  python scripts/fetch_heat.py --date 2026-11-26
  python scripts/fetch_heat.py --input inbox/2026-11-26.jsonl.gz
  python scripts/fetch_heat.py --demo               # 内置样本，无网络也能跑通
  python scripts/fetch_heat.py --no-llm             # 强制词典口径
  python scripts/fetch_heat.py --skip-reference     # 不跑对照车道

退出码：0=正常（含 stale/降级），1=硬失败（无产物且无历史）
"""

import argparse
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import heat as heat_mod  # noqa: E402
import reference_lane  # noqa: E402
from asset_dict import AssetDict  # noqa: E402
from common import (DAILY_DIR, DATA_DIR, DOCS, INBOX_DIR, REPORT_DIR, ROOT,  # noqa: E402
                    clamp, decode, load_config, load_secrets, log, now_cn, parse_ts,
                    read_json, read_jsonl, sha1_short, step, today_str, truncate,
                    write_json, write_text)
from hard_rules import (classify_intent_rule, dedup, detect_marketing,  # noqa: E402
                        engagement, intent_distribution)
from llm_chain import Degraded, LLMChain, classify_intents, extract_entities  # noqa: E402

HISTORY_DAYS = 90
TREND_DAYS = 60


# ---------------------------------------------------------------------------
# 密钥（本地 .env.local，JSON；绝不入仓）
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# 采集件读取与归一化
# ---------------------------------------------------------------------------
def parse_count(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace(",", "").replace("+", "")
    if not s:
        return 0
    mult = 1
    if s.endswith("万") or s.endswith("w") or s.endswith("W"):
        mult, s = 10000, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100000000, s[:-1]
    elif s.endswith("k") or s.endswith("K"):
        mult, s = 1000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return 0


def normalize_note(raw, source="xhs"):
    title = (raw.get("title") or "").strip()
    desc = (raw.get("desc") or raw.get("content") or raw.get("text") or "").strip()
    if not title and not desc:
        return None
    author = raw.get("nickname") or raw.get("user_nickname") or raw.get("user_id") or ""
    author_hash = raw.get("author_hash") or (sha1_short(author) if author else "")
    note_id = str(raw.get("note_id") or raw.get("id") or raw.get("aweme_id")
                  or sha1_short(title + desc, 16))
    ts = parse_ts(raw.get("time") or raw.get("create_time") or raw.get("publish_time")
                  or raw.get("ts") or raw.get("last_update_time"))
    return {
        "id": note_id,
        "title": title,
        "desc": desc,
        "author_hash": author_hash,
        "liked": parse_count(raw.get("liked_count") or raw.get("liked")),
        "collected": parse_count(raw.get("collected_count") or raw.get("collected")),
        "comment": parse_count(raw.get("comment_count") or raw.get("comment")),
        "share": parse_count(raw.get("share_count")),
        "ts": ts.isoformat() if ts else None,
        "_ts": ts,
        "keyword": raw.get("source_keyword") or raw.get("keyword") or "",
        "source": raw.get("source") or source,
        "url": raw.get("note_url") or raw.get("url") or "",
    }


def load_notes(date, args):
    """返回 (notes, meta)。优先级：--input > inbox 当日文件。"""
    candidates = []
    if args.input:
        candidates = [args.input] if os.path.isfile(args.input) else [
            os.path.join(args.input, f) for f in sorted(os.listdir(args.input))]
    else:
        for name in ("%s.jsonl.gz" % date, "%s.jsonl" % date, "%s.json" % date):
            p = os.path.join(INBOX_DIR, name)
            if os.path.exists(p):
                candidates = [p]
                break

    if not candidates:
        return [], {"files": [], "reason": "inbox 无当日采集件"}

    notes, files = [], []
    for p in candidates:
        rows = read_jsonl(p)
        files.append(os.path.basename(p))
        for raw in rows:
            n = normalize_note(raw)
            if n:
                notes.append(n)
    return notes, {"files": files, "reason": ""}


# ---------------------------------------------------------------------------
# LLM 增强
# ---------------------------------------------------------------------------
def llm_enrich(notes, adict, cfg, use_llm=True):
    stats = {"enabled": use_llm, "extract_batches": 0, "classify_batches": 0,
             "failed_batches": 0, "extract_hits": 0, "classified": 0,
             "degraded": False, "models": [], "errors": []}
    chain = LLMChain(cfg, "extract")
    stats["models"] = [b["name"] for b in chain.backends if not b["blocked"]]

    if not use_llm:
        stats["degraded"] = True
        stats["errors"].append("--no-llm 强制词典口径")
        return stats
    if not chain.available:
        stats["degraded"] = True
        stats["errors"].append("无可用 LLM（未配置 Key）→ 词典口径")
        return stats

    limits = cfg["llm"]["limits"]
    batch_size = int(limits.get("batch_size", 20))
    min_len = int(limits.get("min_text_len", 12))

    # 1) 长尾实体抽取：只处理词典完全没命中的
    pending = [n for n in notes if not n.get("_assets")
               and len((n.get("title") or "") + (n.get("desc") or "")) >= min_len]
    pending.sort(key=lambda n: -engagement(n))
    pending = pending[: int(limits.get("max_notes_for_llm", 500))]
    log("  LLM 长尾抽取候选：%d 条" % len(pending))

    for i in range(0, len(pending), batch_size):
        batch = pending[i:i + batch_size]
        snippets = [((n["title"] or "") + " " + (n["desc"] or "")) for n in batch]
        try:
            result = _call_with_retry(extract_entities, chain, snippets,
                                      stats, "抽取", len(batch))
            stats["extract_batches"] += 1
            for idx, names in result.items():
                if idx < 0 or idx >= len(batch):
                    continue
                ids = []
                for name in names:
                    aid = adict.resolve(name)
                    if aid and adict.is_investable(aid) and aid not in ids:
                        ids.append(aid)
                if ids:
                    batch[idx]["_assets"] = {a: 1.0 / len(ids) for a in ids}
                    batch[idx]["_extract"] = "llm"
                    stats["extract_hits"] += 1
        except Degraded as e:
            stats["failed_batches"] += 1
            stats["degraded"] = True
            stats["errors"].append("抽取批失败: %s" % truncate(str(e), 100))
            break
        except Exception as e:                       # 单批失败只丢该批
            stats["failed_batches"] += 1
            stats["errors"].append("抽取批异常: %s" % truncate(str(e), 100))
            continue

    # 2) 意图五分类：优先榜上有望的笔记
    targets = [n for n in notes if n.get("_assets")]
    targets.sort(key=lambda n: (-engagement(n)))
    targets = targets[:300]
    for i in range(0, len(targets), batch_size):
        batch = targets[i:i + batch_size]
        try:
            result = _call_with_retry(classify_intents, chain, batch,
                                      stats, "分类", len(batch))
            stats["classify_batches"] += 1
            for idx, label in result.items():
                if 0 <= idx < len(batch):
                    batch[idx]["intent"] = label
                    stats["classified"] += 1
        except Degraded as e:
            stats["failed_batches"] += 1
            stats["degraded"] = True
            stats["errors"].append("分类批失败: %s" % truncate(str(e), 100))
            break
        except Exception as e:
            stats["failed_batches"] += 1
            stats["errors"].append("分类批异常: %s" % truncate(str(e), 100))
            continue

    return stats


def _call_with_retry(func, chain, payload, stats, kind, n_items):
    """解析空结果时重试一次 —— 模型偶尔只回一句解释、不回 JSON。

    空结果 ≠ 无资产（合法的空数组是正常业务结果），所以只有两种都为空才重试；
    重试后仍空则记一条可追溯的错误，绝不静默。
    """
    last = {}
    for attempt in (1, 2):
        try:
            res = func(chain, payload)
        except Degraded:
            raise
        except Exception as e:
            stats["errors"].append("%s批异常(%d/%d条): %s" % (kind, attempt, n_items, truncate(str(e), 90)))
            res = {}
        if res:
            return res
        last = res
        if attempt == 1:
            log("    ↻ %s批返回无法解析，重试一次" % kind)
    stats["errors"].append("%s批重试后仍无可解析结果（%d 条）" % (kind, n_items))
    return last


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
def build_payload(date, ranked, surge, newbs, ref, lane_tags, stats, quality,
                  caliber, events, candidates, config, stale=False, stale_note=""):
    top_n = int(config["output"]["leaderboard_top"])
    board = []
    for r in ranked[:top_n]:
        row = dict(r)
        row.pop("top_notes", None)
        row["lane"] = lane_tags.get(r["id"], {"tag": "insufficient", "ref_count": None})
        row["top_notes"] = [{
            "title": t["title"], "liked": t["liked"], "collected": t["collected"],
            "comment": t["comment"], "intent": t["intent"],
        } for t in (r.get("top_notes") or [])]
        board.append(row)

    recent_events = [e for e in (events.get("events") or []) if e.get("status") != "closed"][:12]
    return {
        "schema": 2,
        "project": "xhs-heat",
        "date": date,
        "generated_at": now_cn().isoformat(timespec="seconds"),
        "status": "stale" if stale else ("degraded" if caliber.get("degraded") else "ok"),
        "stale": stale,
        "stale_note": stale_note,
        "caliber": caliber,
        "stats": stats,
        "quality": quality,
        "leaderboard": board,
        "surge": surge,
        "newcomers": newbs,
        "events_recent": recent_events,
        "reference": ref,
        "candidates": candidates,
        "disclaimer": config["compliance"]["disclaimer"],
    }


def write_outputs(payload, cfg, date, ranked):
    ts = now_cn().strftime("%Y%m%dT%H%M%S")
    write_json(os.path.join(DOCS, "latest.json"), payload, indent=None)
    write_json(os.path.join(DAILY_DIR, "%s.json" % date), payload, indent=None)

    # 聚合快照（热度层的历史输入）
    agg = {
        "date": date,
        "caliber": payload["caliber"],
        "assets": {r["id"]: {
            "name": r["name"], "volume": r["volume"], "raw_engagement": r["raw_engagement"],
            "heat": r["heat"], "rank": r["rank"], "notes": r["notes"],
            "marketing_share": r["marketing_share"], "intent": r["intent"],
            "category": r["category"],
        } for r in ranked},
    }
    write_json(os.path.join(DATA_DIR, "agg", "%s.json" % date), agg, indent=None)

    # 索引（保留 180 天）
    idx_path = os.path.join(DATA_DIR, "index.json")
    idx = read_json(idx_path, default={"dates": []}) or {"dates": []}
    dates = [d for d in idx.get("dates", []) if d != date]
    dates.insert(0, date)
    dates = sorted(set(dates), reverse=True)[:180]
    write_json(idx_path, {"dates": dates, "updated": date})

    # 趋势（近 60 天，供看板画曲线）
    history, hist_days = heat_mod.load_history(HISTORY_DAYS)
    trends = {}
    for d in hist_days[-TREND_DAYS:]:
        for aid, row in history[d].items():
            trends.setdefault(aid, []).append([d, row.get("heat"), row.get("volume")])
    write_json(os.path.join(DATA_DIR, "trends.json"), {"updated": date, "series": trends})

    # 推送文本
    write_text(os.path.join(DOCS, "push", "%s.txt" % date), render_push(payload, cfg))
    return ts


def render_push(payload, cfg):
    lines = ["📊 小红书投资热度榜 — %s" % payload["date"], ""]
    if payload.get("stale"):
        lines.append("⚠️ %s" % payload.get("stale_note", "今日采集中断，沿用上一期"))
        lines.append("")
    lines.append("🔥 总榜 Top5（自然笔记口径）")
    arrows = {"up": "▲", "down": "▼", "flat": "─", "new": "🆕"}
    for r in payload["leaderboard"][:5]:
        it = r.get("intent") or {}
        lane = (r.get("lane") or {}).get("tag")
        dot = {"double_hot": "🔴", "xhs_only_hot": "🔵", "reference_only_hot": "⚪"}.get(lane, "")
        lines.append(" %d. %s %s %s %s  学%d%% 买%d%% 晒%d%% 愁%d%% 广%d%%" % (
            r["rank"], r["name"], int(r["heat"]), arrows.get(r["trend"], ""), dot,
            round(it.get("学", 0) * 100), round(it.get("买", 0) * 100),
            round(it.get("晒", 0) * 100), round(it.get("愁", 0) * 100),
            round(it.get("广", 0) * 100)))
    if payload["surge"]:
        lines.append("")
        lines.append("🚀 蹿升 Top%d（3日增速）" % len(payload["surge"]))
        for s in payload["surge"]:
            lines.append(" · %s %+.0f%%" % (s["name"], s["growth"] * 100))
    if payload["newcomers"]:
        lines.append("")
        lines.append("🆕 新面孔：" + " / ".join(x["name"] for x in payload["newcomers"]))
    st = payload["stats"]
    lines.append("")
    lines.append("⚠️ 营销占比 %d%%｜有效样本 %d 条｜口径：%s" % (
        round(st.get("marketing_ratio", 0) * 100), st.get("effective", 0),
        {"dict": "词典模式", "dict+llm": "词典+LLM"}.get(payload["caliber"].get("extract"), payload["caliber"].get("extract", ""))))
    lines.append("")
    lines.append(payload["disclaimer"])
    return "\n".join(lines) + "\n"


def build_report(date, args, stats, quality, llm_stats, ref, caliber, elapsed, keyword_counts):
    return {
        "ts": now_cn().isoformat(timespec="seconds"),
        "date": date,
        "args": {k: v for k, v in vars(args).items()},
        "elapsed_s": round(elapsed, 1),
        "stats": stats,
        "quality": quality,
        "llm": llm_stats,
        "caliber": caliber,
        "keywords": keyword_counts,
        "reference": {
            "ok": (ref or {}).get("ok"),
            "sources": (ref or {}).get("sources"),
            "gate": (ref or {}).get("gate"),
        },
        "notes": "每词通路/条数见 keywords；LLM 降级原因见 llm.errors",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run(args):
    t0 = time.time()
    load_secrets()
    cfg = load_config()
    date = args.date or today_str()

    step("1/8 读取采集件 · %s" % date)
    notes_raw, meta = load_notes(date, args)
    if args.demo and not notes_raw:
        notes_raw = [normalize_note(r) for r in demo_notes()]
        meta = {"files": ["(demo)"], "reason": "内置样本"}
    if not notes_raw:
        return carry_over(cfg, date, args, t0, meta)
    log("  读入 %d 条 · 文件 %s" % (len(notes_raw), ", ".join(meta["files"]) or "-"))

    step("2/8 硬规则：时间窗 / 去重 / 营销识别")
    window = int(cfg["hard_rules"].get("time_window_days", 1))
    notes, window_used = _window(notes_raw, date, window)
    if len(notes) < int(cfg["quality_gates"]["min_effective_notes"]) and window < 3:
        notes, window_used = _window(notes_raw, date, 3)
        if window_used > window:
            log("  ↳ 24h 窗口样本不足，放宽到 %d 天窗口（已在报告标注）" % window_used)

    notes, dropped = dedup(notes)
    mstats = detect_marketing(notes, cfg)
    log("  时间窗 %d 天 · 窗口内 %d 条 · 标题去重 -%d · 营销 %d 条（%.0f%%）"
        % (window_used, len(notes), dropped, mstats["marketing"], mstats["ratio"] * 100))

    keyword_counts = {}
    for n in notes:
        for kw in (n.get("keyword") or "").split(","):
            kw = kw.strip()
            if kw:
                keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

    step("3/8 词典匹配（硬规则层）")
    adict = AssetDict()
    hit_notes = 0
    for n in notes:
        r = adict.match((n.get("title") or "") + " " + (n.get("desc") or ""))
        ids = [m["id"] for m in r["matched"]]
        n["_conf"] = max([m["conf"] for m in r["matched"]] or [0])
        if ids:
            n["_assets"] = {a: 1.0 / len(ids) for a in ids}
            n["_extract"] = "dict"
            hit_notes += 1
        else:
            n["_assets"] = {}
        n["_candidates"] = r["candidates"]
    log("  词典命中 %d / %d 条（%.0f%%）" % (hit_notes, len(notes), hit_notes * 100.0 / max(1, len(notes))))

    step("4/8 LLM 长尾抽取 + 意图分类")
    llm_stats = llm_enrich(notes, adict, cfg, use_llm=not args.no_llm)
    log("  批次 抽取%d / 分类%d / 失败%d · 模型 %s"
        % (llm_stats["extract_batches"], llm_stats["classify_batches"],
           llm_stats["failed_batches"], ",".join(llm_stats["models"]) or "无"))

    # 意图兜底
    for n in notes:
        if not n.get("intent"):
            n["intent"] = classify_intent_rule(n, cfg)

    step("5/8 聚合 + 热度分")
    rows = heat_mod.aggregate(notes, adict, cfg)
    history, hist_days = heat_mod.load_history(HISTORY_DAYS, exclude=date)
    ranked, caliber = heat_mod.compute_heat(rows, history, cfg, date)
    ranked = heat_mod.annotate_trends(ranked, history, adict, date)
    surge = heat_mod.surge_list(ranked, history, cfg, int(cfg["output"]["surge_top"]))
    newbs = heat_mod.newcomers(ranked, history, cfg) if cfg["output"].get("newcomers") else []
    log("  资产 %d 个 · 口径 %s（历史 %d 天）" % (len(ranked), caliber["mode"], caliber["history_days"]))
    for r in ranked[:5]:
        log("    %d. %-10s %5.1f  笔记%3d 互动%6d 营销%2.0f%%"
            % (r["rank"], r["name"], r["heat"], r["notes"], r["raw_engagement"], r["marketing_share"] * 100))

    step("6/8 对照车道（雪球热帖 + 东财人气榜）")
    ref = {"ok": False, "note": "已跳过"}
    if not args.skip_reference and cfg["lanes"]["reference"].get("enabled"):
        try:
            ref = reference_lane.build(cfg, adict)
            ref["generated_at"] = now_cn().isoformat(timespec="seconds")
        except Exception as e:
            ref = {"ok": False, "note": "异常: %s" % truncate(str(e), 120)}
            log("  ⚠️ 对照车道异常，不影响主榜：%s" % e)
    lane_tags = reference_lane.tags_for(ranked, ref, cfg)

    step("7/8 事件库")
    events, changes = heat_mod.update_events(ranked, history, cfg, date)
    log("  事件库 %d 条 · 新建 %d / 更新 %d / 淘汰 %d"
        % (len(events["events"]), len(changes["created"]),
           len(changes["updated"]), changes["evicted"]))

    step("8/8 质量闸门 + 落盘")
    effective = sum(1 for n in notes if n["_assets"] and not n.get("marketing"))
    quality = quality_gates(notes, mstats, effective, llm_stats, cfg, window_used)
    caliber["extract"] = "dict+llm" if (llm_stats["extract_batches"] and not llm_stats["degraded"]) else "dict"
    caliber["intent"] = "llm" if llm_stats["classified"] > 0 else "rule"
    caliber["window_days"] = window_used
    caliber["degraded"] = bool(llm_stats["degraded"])

    candidates = _top_candidates(notes, adict)
    stats = {
        "raw_notes": len(notes_raw),
        "window_notes": len(notes),
        "effective": effective,
        "marketing_ratio": mstats["ratio"],
        "dict_hit_notes": hit_notes,
        "llm_extract_notes": llm_stats["extract_hits"],
        "assets": len(ranked),
        "keywords": len(keyword_counts),
        "window_days": window_used,
        "dropped_dup": dropped,
    }
    payload = build_payload(date, ranked, surge, newbs, ref, lane_tags, stats, quality,
                            caliber, events, candidates, cfg)
    ts = write_outputs(payload, cfg, date, ranked)

    report = build_report(date, args, stats, quality, llm_stats, ref, caliber,
                          time.time() - t0, keyword_counts)
    write_json(os.path.join(REPORT_DIR, "%s.json" % ts), report, indent=None)
    write_step_summary(payload, quality, llm_stats, ref)

    log("")
    log("✅ 完成 · %s" % date)
    log("   榜单前五：%s" % "、".join("%s(%.0f)" % (r["name"], r["heat"]) for r in ranked[:5]))
    log("   看板产物：docs/latest.json / docs/daily/%s.json / docs/data/agg/%s.json" % (date, date))
    log("   耗时 %.1fs" % (time.time() - t0))
    return 0


def _window(notes, date, days):
    """保留 [date-days, date] 窗口内的笔记；无时间的保留（宁可多算也不静默丢）。"""
    base = parse_ts(date)
    if base is None:
        return notes, days
    cutoff = base.timestamp() - days * 86400
    kept = []
    for n in notes:
        ts = n.get("_ts")
        if ts is None or ts.timestamp() >= cutoff:
            kept.append(n)
    return kept, days


def _top_candidates(notes, adict, top=15):
    from collections import Counter
    c = Counter()
    for n in notes:
        for frag in (n.get("_candidates") or []):
            c[frag] += 1
    out = []
    for frag, cnt in c.most_common(top):
        if adict.resolve(frag):
            continue
        out.append({"frag": frag, "count": cnt})
    return out


def quality_gates(notes, mstats, effective, llm_stats, cfg, window_days):
    q = cfg["quality_gates"]
    checks = [
        {"name": "有效笔记数", "value": effective, "threshold": q["min_effective_notes"],
         "ok": effective >= int(q["min_effective_notes"]),
         "note": "低于阈值标『样本不足』，榜单照常展示"},
        {"name": "营销占比", "value": round(mstats["ratio"], 3), "threshold": q["marketing_ratio_warn"],
         "ok": mstats["ratio"] <= float(q["marketing_ratio_warn"]), "note": "超过则全榜标『投放密集期』"},
        {"name": "词典命中率", "value": round(sum(1 for n in notes if n.get("_extract") == "dict") / float(max(1, len(notes))), 3),
         "threshold": 0.5, "ok": sum(1 for n in notes if n.get("_extract") == "dict") >= len(notes) * 0.5,
         "note": "低于 50% 说明词典滞后，需周度补词"},
        {"name": "LLM 链路", "value": "降级" if llm_stats["degraded"] else "正常",
         "threshold": "正常", "ok": not llm_stats["degraded"], "note": "；".join(llm_stats["errors"][:2])},
        {"name": "时间窗", "value": "%d 天" % window_days, "threshold": "1 天",
         "ok": window_days <= 1, "note": "放宽窗口会稀释『今日热度』，仅作样本兜底"},
    ]
    return {"passed": all(c["ok"] for c in checks), "checks": checks}


def write_step_summary(payload, quality, llm_stats, ref):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        lines = ["## xhs-heat · %s" % payload["date"], ""]
        lines.append("状态 **%s**｜口径 **%s**｜有效样本 %d｜营销 %d%%"
                     % (payload["status"], payload["caliber"].get("extract"),
                        payload["stats"]["effective"],
                        round(payload["stats"]["marketing_ratio"] * 100)))
        lines.append("")
        lines.append("| # | 资产 | 热度 | 趋势 | 笔记 | 互动 | 营销占比 |")
        lines.append("|---|---|---|---|---|---|---|")
        for r in payload["leaderboard"][:10]:
            lines.append("| %d | %s | %.1f | %s | %d | %d | %.0f%% |"
                         % (r["rank"], r["name"], r["heat"], r["trend"], r["notes"],
                            r["raw_engagement"], r["marketing_share"] * 100))
        lines.append("")
        for c in quality["checks"]:
            lines.append("- %s %s：%s（阈值 %s）" % ("✅" if c["ok"] else "⚠️", c["name"], c["value"], c["threshold"]))
        if ref and ref.get("sources"):
            lines.append("")
            lines.append("对照车道：" + "；".join("%s %s" % (s["name"], "OK" if s["ok"] else "失败")
                                                 for s in ref["sources"]))
        if llm_stats.get("errors"):
            lines.append("")
            lines.append("LLM 事件：" + "；".join(llm_stats["errors"][:3]))
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log("  ⚠️ Step Summary 写入失败：%s" % e)


# ---------------------------------------------------------------------------
# 无当日采集件：沿用上一期 + 水印（v3 降级 2）
# ---------------------------------------------------------------------------
def carry_over(cfg, date, args, t0, meta):
    step("采集中断：沿用上一期 + 水印")
    prev = read_json(os.path.join(DOCS, "latest.json"))
    if not prev:
        log("❌ 无当日采集件，也没有历史产物 → 无法出榜")
        log("   采集件应放在 inbox/%s.jsonl(.gz)" % date)
        return 1

    prev_date = prev.get("date")
    prev["status"] = "stale"
    prev["stale"] = True
    prev["stale_note"] = "今日（%s）未收到采集件（%s），以下沿用 %s 的榜单，仅供参照" % (
        date, meta.get("reason", "inbox 为空"), prev_date)
    prev["generated_at"] = now_cn().isoformat(timespec="seconds")

    adict = AssetDict()
    try:
        ref = reference_lane.build(cfg, adict)
        prev["reference"] = ref
        log("  对照车道已刷新（%s）" % ("OK" if ref.get("ok") else "失败"))
    except Exception as e:
        log("  ⚠️ 对照车道刷新失败：%s" % e)

    write_json(os.path.join(DOCS, "latest.json"), prev, indent=None)
    write_text(os.path.join(DOCS, "push", "%s.txt" % date), render_push(prev, cfg))
    report = {"ts": now_cn().isoformat(timespec="seconds"), "date": date,
              "status": "stale", "reason": meta.get("reason"),
              "carried_from": prev_date, "elapsed_s": round(time.time() - t0, 1)}
    write_json(os.path.join(REPORT_DIR, now_cn().strftime("%Y%m%dT%H%M%S") + ".json"), report, indent=None)
    log("✅ 已输出 stale 版看板（源：%s）" % prev_date)
    return 0


# ---------------------------------------------------------------------------
# 内置样本（离线验证用；字段与 MediaCrawler 输出一致）
# ---------------------------------------------------------------------------
def demo_notes():
    base = now_cn().timestamp()
    rows = [
        ("黄金又新高了，518880还能追吗", "金价突破，定投黄金ETF三年了，今天又加了一笔", 4200, 1800, 320, "黄金"),
        ("攒金豆一年，攒出一根小金条", "每月一颗金豆，积存金真香，比买金饰划算", 8600, 5200, 640, "金豆"),
        ("备婚三金，金镯子怎么选", "婆家要求三金，金饰现在这个价格合适吗", 1200, 300, 180, "黄金"),
        ("纳指QDII还能上车吗", "016452 定投两年，收益不错，现在回调要不要加仓", 3100, 900, 410, "纳指"),
        ("美股开户流程记录", "折腾了一周终于开通，记录一下流程给需要的朋友", 900, 700, 120, "美股"),
        ("半导体这波行情怎么理解", "512480 从底部起来不少了，国产替代逻辑还在吗", 1500, 400, 260, "半导体"),
        ("算力租赁需求爆炸", "AIDC 和光模块哪个更受益，CPO 技术路线怎么看", 700, 200, 90, "算力"),
        ("存款利率又降了", "三年期大额存单只剩2出头，钱还能放哪", 5200, 2100, 880, "存款"),
        ("债基也会亏钱吗", "纯债基金最近回撤，第一次买债基有点慌", 2800, 1100, 430, "债基"),
        ("大饼又突破前高", "币圈现在什么情况，屯币党还安全吗", 1900, 600, 520, "比特币"),
        ("恒生科技ETF解套了", "513180 拿了两年终于回本，要不要走", 2300, 800, 340, "恒生科技"),
        ("红利低波是真的稳", "红利ETF分红到账，收息的感觉真好", 1700, 950, 210, "红利"),
        ("加微领基金定投入门资料", "内部课最后3个名额，扫码进群领资料", 120, 30, 40, "基金"),
        ("普通人逆袭就靠这个", "错过再等十年，下一个风口已经来了，点击私信我", 80, 20, 15, "基金"),
        ("求问红利和债基怎么配", "想要稳定的收息组合，红利低波加纯债合理吗", 600, 180, 75, "红利"),
        ("新能源车还能拿吗", "锂电跌了很久，固态电池是不是新故事", 1100, 350, 190, "新能源"),
    ]
    out = []
    for i, (title, desc, liked, collected, comment, kw) in enumerate(rows):
        out.append({
            "note_id": "demo%02d" % i, "title": title, "desc": desc,
            "liked_count": liked, "collected_count": collected, "comment_count": comment,
            "time": int((base - i * 900) * 1000), "nickname": "demo_user_%d" % (i % 5),
            "source_keyword": kw, "note_url": "",
        })
    return out


# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="xhs-heat 热度处理与发布")
    ap.add_argument("--date", help="报告日期 YYYY-MM-DD，默认今天")
    ap.add_argument("--input", help="采集件路径（.jsonl / .jsonl.gz / 目录）")
    ap.add_argument("--demo", action="store_true", help="使用内置样本（离线验证）")
    ap.add_argument("--no-llm", action="store_true", help="强制词典口径")
    ap.add_argument("--skip-reference", action="store_true", help="跳过对照车道")
    args = ap.parse_args(argv)
    try:
        return run(args)
    except Exception:
        log("❌ 未捕获异常：")
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
