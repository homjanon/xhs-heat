# -*- coding: utf-8 -*-
"""
xhs-heat · heat.py
聚合 + 热度分 + 事件库（v2 第 05/07 节，ai-radar 双留存同构）

热度公式：
  Heat = 100 × (0.45·V + 0.35·E + 0.20·M)
  V = 讨论量（自然笔记数，多资产按 1/n 分摊）
  E = 互动量（赞×1 + 藏×3 + 评×2 的对数和，单条贡献上限 10%）
  M = 3 日动量（截断 [-2, +3]）

两种口径：
  rolling   —— 历史 ≥ bootstrap_min_days(8) 天，用 90 日滚动分位数归一
  bootstrap —— 历史不足，用当日截面 log 归一 + 动量中性 0.5，产物标 caliber=bootstrap
               （绝不用「第 1 天就满分 100」这种假精度糊过去）
"""

import math
import os

from common import DATA_DIR, clamp, log, read_json, today_str, write_json
from hard_rules import INTENTS, intent_distribution

AGG_DIR = os.path.join(DATA_DIR, "agg")


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def aggregate(notes, adict, cfg):
    """notes 需已带 _assets={asset_id: weight}、intent、marketing 字段。"""
    buckets = {}
    for n in notes:
        for aid, weight in (n.get("_assets") or {}).items():
            if not adict.is_investable(aid):
                continue
            b = buckets.setdefault(aid, {"natural": [], "marketing": [], "weights": {}})
            key = id(n)
            b["weights"][key] = weight
            if n.get("marketing"):
                b["marketing"].append(n)
            else:
                b["natural"].append(n)

    cap = float(cfg["hard_rules"].get("single_note_heat_cap", 0.1))
    ew = cfg["heat"]["engagement_weights"]
    log_base = float(cfg["heat"].get("engagement_log_base", 10))

    rows = {}
    for aid, b in buckets.items():
        weights = b["weights"]
        natural = b["natural"]
        if not natural:
            continue
        total_notes = len(natural) + len(b["marketing"])

        volume = sum(weights.get(id(n), 1.0) for n in natural)

        # 单条贡献上限：先算原始互动，再把超过 cap 的巨头压回 cap 后重算
        contrib = []
        for n in natural:
            w = weights.get(id(n), 1.0)
            e = (int(n.get("liked") or 0) * ew.get("like", 1)
                 + int(n.get("collected") or 0) * ew.get("collect", 3)
                 + int(n.get("comment") or 0) * ew.get("comment", 2))
            contrib.append((n, e * w))
        raw_total = sum(c for _n, c in contrib)
        capped_total = raw_total
        single_share = 0.0
        if raw_total > 0 and cap > 0:
            top = max(contrib, key=lambda x: x[1])
            single_share = top[1] / raw_total
            if single_share > cap:
                capped_total = raw_total - top[1] + raw_total * cap
        engagement = math.log(1.0 + capped_total, log_base) if capped_total > 0 else 0.0

        intent = {k: 0.0 for k in INTENTS}
        for n in natural:
            intent[n.get("intent") or "学"] += weights.get(id(n), 1.0)
        intent_sum = sum(intent.values()) or 1.0
        intent = {k: round(v / intent_sum, 3) for k, v in intent.items()}

        top_notes = sorted(natural, key=lambda n: -(int(n.get("liked") or 0)
                                                   + int(n.get("collected") or 0) * 3
                                                   + int(n.get("comment") or 0) * 2))[:3]
        asset = adict.assets.get(aid, {})
        rows[aid] = {
            "id": aid,
            "name": adict.name_of(aid),
            "category": asset.get("category", ""),
            "volume": round(volume, 2),
            "notes": len(natural),
            "engagement": round(engagement, 3),
            "raw_engagement": int(raw_total),
            "marketing_notes": len(b["marketing"]),
            "marketing_share": round(len(b["marketing"]) / float(total_notes), 3) if total_notes else 0.0,
            "intent": intent,
            "single_note_share": round(single_share, 3),
            "top_notes": [{
                "id": n.get("id"),
                "title": (n.get("title") or n.get("desc") or "")[:80],
                "liked": int(n.get("liked") or 0),
                "collected": int(n.get("collected") or 0),
                "comment": int(n.get("comment") or 0),
                "intent": n.get("intent"),
            } for n in top_notes],
        }
    return rows


# ---------------------------------------------------------------------------
# 历史
# ---------------------------------------------------------------------------
def load_history(days=90, exclude=None):
    """读 docs/data/agg/*.json，返回 {date: {asset_id: row}} 与日期列表。"""
    out = {}
    if not os.path.isdir(AGG_DIR):
        return out, []
    files = sorted(f for f in os.listdir(AGG_DIR) if f.endswith(".json"))
    for f in files[-days:]:
        d = f[:-5]
        if exclude and d == exclude:
            continue
        obj = read_json(os.path.join(AGG_DIR, f))
        if obj and obj.get("assets"):
            out[d] = obj["assets"]
    return out, sorted(out.keys())


# ---------------------------------------------------------------------------
# 热度
# ---------------------------------------------------------------------------
def _percentile_rank(value, series):
    if not series:
        return 0.5
    below = sum(1 for x in series if x <= value)
    return below / float(len(series))


def compute_heat(rows, history, cfg, date=None):
    """返回 (带 heat/rank 的行列表, 口径信息)"""
    date = date or today_str()
    min_days = int(cfg["heat"].get("bootstrap_min_days", 8))
    hist_days = sorted(history.keys())
    rolling = len(hist_days) >= min_days

    max_v = max([r["volume"] for r in rows.values()] or [1.0]) or 1.0
    max_e = max([r["raw_engagement"] for r in rows.values()] or [1]) or 1

    for aid, r in rows.items():
        series_v, series_e = [], []
        for d in hist_days[-90:]:
            h = history[d].get(aid)
            if h:
                series_v.append(float(h.get("volume") or 0))
                series_e.append(float(h.get("raw_engagement") or 0))

        use_rolling = rolling and len(series_v) >= min_days
        if use_rolling:
            v_norm = _percentile_rank(r["volume"], series_v)
            e_norm = _percentile_rank(r["raw_engagement"], series_e)
            v3 = series_v[-3] if len(series_v) >= 3 else 0.0
            momentum = (r["volume"] + 1.0) / (v3 + 1.0) - 1.0
            lo, hi = cfg["hard_rules"].get("momentum_clip", [-2, 3])
            momentum = clamp(momentum, float(lo), float(hi))
            m_norm = (momentum + 2.0) / 5.0
            r["momentum"] = round(momentum, 3)
        else:
            v_norm = math.log1p(r["volume"]) / math.log1p(max_v) if max_v > 0 else 0.0
            e_norm = math.log1p(r["raw_engagement"]) / math.log1p(max_e) if max_e > 0 else 0.0
            r["momentum"] = None
            m_norm = 0.5

        w = cfg["heat"]["weights"]
        heat = 100.0 * (w["volume"] * v_norm + w["engagement"] * e_norm + w["momentum"] * m_norm)
        r["v_norm"] = round(v_norm, 3)
        r["e_norm"] = round(e_norm, 3)
        r["m_norm"] = round(m_norm, 3)
        r["heat"] = round(clamp(heat, 0, 100), 1)
        r["caliber"] = "rolling" if use_rolling else "bootstrap"

    ranked = sorted(rows.values(), key=lambda r: (-r["heat"], -r["volume"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    caliber = {
        "mode": "rolling" if rolling else "bootstrap",
        "history_days": len(hist_days),
        "min_days_required": min_days,
        "note": ("历史 %d 天 ≥ %d，用 90 日滚动分位数归一" % (len(hist_days), min_days)) if rolling
                else ("历史仅 %d 天（< %d），走冷启动截面归一；动量按中性 0.5 处理，"
                      "热分数值不可与日后 rolling 口径直接比较" % (len(hist_days), min_days)),
    }
    return ranked, caliber


# ---------------------------------------------------------------------------
# 趋势 / 连续上榜
# ---------------------------------------------------------------------------
def annotate_trends(ranked, history, adict, date=None):
    """▲▼─ / 连续上榜天数 / 首次出现日期。

    没有历史（首日）时不做趋势判断：全部 flat、delta=None，
    因为「昨天没有可比数据」和「昨天没上榜」是两件不同的事。
    """
    date = date or today_str()
    hist_days = sorted(history.keys())
    has_history = bool(hist_days)

    for r in ranked:
        aid = r["id"]
        prev_heat = None
        for d in reversed(hist_days):
            h = history[d].get(aid)
            if h and h.get("heat") is not None:
                prev_heat = float(h["heat"])
                break

        if prev_heat is None:
            r["trend"] = "new" if has_history else "flat"
            r["delta_heat"] = None
        else:
            delta = r["heat"] - prev_heat
            r["delta_heat"] = round(delta, 1)
            r["trend"] = "up" if delta >= 3 else ("down" if delta <= -3 else "flat")

        streak = 1
        for d in reversed(hist_days):
            if history[d].get(aid):
                streak += 1
            else:
                break
        r["streak_days"] = streak

        first_seen = None
        for d in hist_days:
            if history[d].get(aid):
                first_seen = d
                break
        r["first_seen"] = first_seen or date
    return ranked


# ---------------------------------------------------------------------------
# 动量榜（蹿升）
# ---------------------------------------------------------------------------
def surge_list(ranked, history, cfg, top=3):
    """按 3 日动量排序；冷启动时按「相对昨日增量」兜底。"""
    hist_days = sorted(history.keys())
    out = []
    if hist_days:
        for r in ranked:
            series = [float(history[d][r["id"]]["volume"]) for d in hist_days[-3:]
                      if history[d].get(r["id"])]
            if not series:
                continue
            base = sum(series) / len(series)
            if base < 1:
                continue
            growth = (r["volume"] - base) / base
            if growth <= 0.05 or r["volume"] < 3:
                continue
            out.append({
                "id": r["id"], "name": r["name"], "growth": round(growth, 2),
                "volume": r["volume"], "heat": r["heat"], "intent": r["intent"],
                "caliber": "3日基线" if len(series) >= 3 else "%d日基线" % len(series),
            })
    out.sort(key=lambda x: -x["growth"])
    return out[:top]


# ---------------------------------------------------------------------------
# 新面孔
# ---------------------------------------------------------------------------
def newcomers(ranked, history, cfg, top=5):
    """首日不列新面孔：没有历史就没有「新」的概念，全是新的等于全不是新的。"""
    out = []
    hist_days = sorted(history.keys())
    if not hist_days:
        return out
    for r in ranked[:30]:
        if history and any(history[d].get(r["id"]) for d in hist_days):
            continue
        if r["volume"] < 2:
            continue
        out.append({"id": r["id"], "name": r["name"], "heat": r["heat"],
                    "volume": r["volume"], "rank": r["rank"]})
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------------------
# 事件库（严口径）
# ---------------------------------------------------------------------------
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")


def load_events():
    obj = read_json(EVENTS_PATH, default={"version": 1, "events": []})
    if not isinstance(obj, dict):
        obj = {"version": 1, "events": []}
    obj.setdefault("events", [])
    return obj


def update_events(ranked, history, cfg, date=None):
    """按 v3 第 07 节三条门槛入库；容量淘汰；返回 (events_obj, 本次变动)

    纪律：
      - 首日（无历史）不判 newcomer 规则 —— 「第一天全榜都是新面孔」是数据事实，不是事件
      - 动量分位需 ≥20 日样本，判不了就如实跳过，不硬判
    """
    from datetime import date as _date

    date = date or today_str()
    store = load_events()
    events = store["events"]
    rules = {r["rule"]: r for r in cfg["events"]["entry_rules"]}
    changes = {"created": [], "updated": [], "evicted": 0, "closed": 0, "skipped_rules": []}
    hist_days = sorted(history.keys())

    heat_series, vol_series = {}, {}
    for d in hist_days:
        for aid, h in history[d].items():
            heat_series.setdefault(aid, []).append(float(h.get("heat") or 0))
            vol_series.setdefault(aid, []).append(float(h.get("volume") or 0))
    for r in ranked:
        heat_series.setdefault(r["id"], []).append(float(r["heat"]))
        vol_series.setdefault(r["id"], []).append(float(r["volume"]))

    def _consecutive(aid, days, threshold):
        s = heat_series.get(aid) or []
        return len(s) >= days and all(v >= threshold for v in s[-days:])

    def _momentum_pct(aid, percentile):
        s = vol_series.get(aid) or []
        if len(s) < 20:
            return None
        moms = []
        for i in range(3, len(s)):
            moms.append((s[i] + 1.0) / (s[i - 3] + 1.0) - 1.0)
        today_m = moms[-1]
        rank = sum(1 for m in moms if m <= today_m) / float(len(moms))
        return rank >= percentile

    open_by_asset = {e["asset_id"]: e for e in events if e.get("status") != "closed"}

    for r in ranked:
        aid = r["id"]
        reasons = []

        r1 = rules.get("heat_ge_consecutive")
        if r1 and _consecutive(aid, int(r1["consecutive_days"]), float(r1["heat"])):
            reasons.append("热度≥%d 连续 %d 日" % (r1["heat"], r1["consecutive_days"]))

        r2 = rules.get("momentum_percentile")
        if r2:
            hit = _momentum_pct(aid, float(r2["percentile"]))
            if hit is True:
                reasons.append("单日增速进历史前 %d%%" % round((1 - r2["percentile"]) * 100))
            elif hit is None and hist_days and "动量分位需 ≥20 日样本，本期跳过" not in changes["skipped_rules"]:
                changes["skipped_rules"].append("动量分位需 ≥20 日样本，本期跳过")

        r3 = rules.get("newcomer_fast")
        if r3 and hist_days and r["rank"] <= int(r3["reach_top"]):
            if r.get("first_seen") == date and r.get("streak_days", 99) <= int(r3["within_days"]):
                reasons.append("首次出现即进 Top%d" % r3["reach_top"])

        e = open_by_asset.get(aid)
        if not reasons:
            continue
        if e:
            e["last_date"] = date
            e["miss_days"] = 0
            if r["heat"] > float(e.get("peak_heat") or 0):
                e["peak_heat"] = r["heat"]
                e["peak_date"] = date
            try:
                e["span_days"] = (_date.fromisoformat(date)
                                  - _date.fromisoformat(e["start_date"])).days + 1
            except (ValueError, KeyError):
                e["span_days"] = e.get("span_days", 1)
            for reason in reasons:
                if reason not in (e.get("entry_reasons") or []):
                    e.setdefault("entry_reasons", []).append(reason)
            changes["updated"].append(aid)
        else:
            ev = {
                "asset_id": aid,
                "name": r["name"],
                "category": r["category"],
                "start_date": date,
                "last_date": date,
                "span_days": 1,
                "peak_heat": r["heat"],
                "peak_date": date,
                "entry_reasons": reasons,
                "intent_peak": r["intent"],
                "status": "open",
            }
            events.append(ev)
            open_by_asset[aid] = ev
            changes["created"].append(aid)

    today_ids = {r["id"] for r in ranked}
    for e in events:
        if e.get("status") == "closed":
            continue
        if e["asset_id"] in today_ids:
            e["miss_days"] = 0
        else:
            e["miss_days"] = int(e.get("miss_days") or 0) + 1
            if e["miss_days"] >= 3:
                e["status"] = "closed"
                e["closed_date"] = date
                changes["closed"] += 1

    cap = int(cfg["events"].get("capacity", 300))
    if len(events) > cap:
        events.sort(key=lambda x: -float(x.get("peak_heat") or 0))
        changes["evicted"] = len(events) - cap
        del events[cap:]

    store["events"] = sorted(events, key=lambda x: (-float(x.get("peak_heat") or 0)))
    store["updated"] = date
    store["version"] = 2
    write_json(EVENTS_PATH, store, indent=None)
    return store, changes
