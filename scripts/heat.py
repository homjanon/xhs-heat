# -*- coding: utf-8 -*-
"""
xhs-heat · heat.py
聚合 + 热度分 + 事件库

v3 修订（2026-11-27，按代码评审核查后重写）：
  1. 所有时序计算一律「日历对齐」：缺数据就是缺，不再把压缩序列当成连续日
  2. V/E 正交化：广度 = 去重作者数，深度 = 单篇互动中位数（原来 V=笔记数、E=互动总和，两者共线）
  3. bootstrap 天数 8 → 30，切换日标 trend="rebased"、不算 delta_heat
  4. 单条 cap 改为迭代收敛，注释写 10% 就真是 10%

Heat = 100 × (w_b·广度 + w_d·深度 + w_m·动量)
"""

import math
import os
from datetime import date as _date
from datetime import timedelta

from common import DATA_DIR, clamp, log, read_json, today_str, write_json
from hard_rules import INTENTS

AGG_DIR = os.path.join(DATA_DIR, "agg")
EVENTS_PATH = os.path.join(DATA_DIR, "events.json")


# ---------------------------------------------------------------------------
# 日历工具
# ---------------------------------------------------------------------------
def date_minus(date, n):
    return (_date.fromisoformat(date) - timedelta(days=int(n))).isoformat()


def at(history, asset_id, date):
    """取某资产在某一天的行；没有就是 None（不做任何"就近替代"）。"""
    return (history.get(date) or {}).get(asset_id)


def _pick(row, *keys, default=0.0):
    for k in keys:
        if row and row.get(k) is not None:
            return float(row[k])
    return default


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------
def _cap_iterative(contrib, cap, iterations=2):
    """把最大单条压到占总量 ≤cap 后重新归一，迭代收敛。

    原来的一步公式 `raw - top + raw*cap` 分母同时变小，实际占比 >cap，
    注释写 10% 却不是 10%。这里迭代到收敛。
    """
    total = sum(c for _n, c in contrib)
    if total <= 0 or cap <= 0 or not contrib:
        return total, 0.0
    if len(contrib) < 2:            # 只有一条时比例必然是 100%，无可压缩
        return total, 1.0
    for _ in range(max(1, iterations)):
        if total <= 0:
            break
        top = max(contrib, key=lambda x: x[1])[1]
        if top / total <= cap:
            break
        others = total - top
        if others <= 0:
            break
        total = others / (1.0 - cap)
        contrib = [(n, min(c, total * cap)) for n, c in contrib]
    return total, (max(c for _n, c in contrib) / total if total > 0 else 0.0)


def aggregate(notes, adict, cfg):
    """notes 需已带 _assets={asset_id: weight}、intent、marketing、author_hash。"""
    buckets = {}
    for n in notes:
        for aid, weight in (n.get("_assets") or {}).items():
            if not adict.is_investable(aid):
                continue
            b = buckets.setdefault(aid, {"natural": [], "marketing": [], "weights": {}})
            b["weights"][id(n)] = weight
            (b["marketing"] if n.get("marketing") else b["natural"]).append(n)

    cap = float(cfg["hard_rules"].get("single_note_heat_cap", 0.1))
    cap_iter = int(cfg["heat"].get("cap_iterations", 2))
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

        # 广度：去重作者数（没有 author_hash 时退回笔记数）
        authors = {n.get("author_hash") for n in natural if n.get("author_hash")}
        breadth = float(len(authors)) if authors else float(len(natural))

        # 深度：单篇互动中位数（原始值），衡量"每条内容有多重"
        per_note, contrib = [], []
        for n in natural:
            w = weights.get(id(n), 1.0)
            e = (int(n.get("liked") or 0) * ew.get("like", 1)
                 + int(n.get("collected") or 0) * ew.get("collect", 3)
                 + int(n.get("comment") or 0) * ew.get("comment", 2))
            per_note.append(e)
            contrib.append((n, e * w))
        per_note.sort()
        mid = len(per_note) // 2
        median_eng = float(per_note[mid]) if per_note else 0.0
        if len(per_note) % 2 == 0 and per_note:
            median_eng = (per_note[mid - 1] + per_note[mid]) / 2.0

        raw_total = sum(c for _n, c in contrib)
        capped_total, single_share = _cap_iterative(contrib, cap, cap_iter)
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
            "volume": round(volume, 2),           # 加权讨论量（多资产按 1/n 分摊）
            "notes": len(natural),
            "breadth": breadth,                    # 去重作者数 → 热度里的"广度"
            "median_engagement": median_eng,       # 单篇互动中位数 → 热度里的"深度"
            "engagement": round(engagement, 3),    # 对数互动（展示用，不进热度）
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
    return sum(1 for x in series if x <= value) / float(len(series))


def compute_heat(rows, history, cfg, date=None):
    date = date or today_str()
    min_days = int(cfg["heat"].get("bootstrap_min_days", 30))
    w = cfg["heat"]["weights"]
    lo, hi = cfg["hard_rules"].get("momentum_clip", [-2, 3])
    hist_days = sorted(history.keys())
    rolling = len(hist_days) >= min_days

    max_b = max([r["breadth"] for r in rows.values()] or [1.0]) or 1.0
    max_d = max([r["median_engagement"] for r in rows.values()] or [1.0]) or 1.0

    for aid, r in rows.items():
        series_b, series_d = [], []
        for d in hist_days[-90:]:
            h = history[d].get(aid)
            if h:
                series_b.append(_pick(h, "breadth", "volume"))
                series_d.append(_pick(h, "median_engagement", "raw_engagement"))

        use_rolling = rolling and len(series_b) >= min_days
        if use_rolling:
            b_norm = _percentile_rank(r["breadth"], series_b)
            d_norm = _percentile_rank(r["median_engagement"], series_d)
            # 动量：严格取「日历 3 天前」，没有就是没有，不做就近替代
            prev = at(history, aid, date_minus(date, 3))
            if prev is None:
                momentum, basis, m_norm = None, "missing_3d", 0.5
            else:
                momentum = (r["breadth"] + 1.0) / (_pick(prev, "breadth", "volume") + 1.0) - 1.0
                momentum = clamp(momentum, float(lo), float(hi))
                basis = "calendar_3d"
                m_norm = (momentum + 2.0) / 5.0
            r["momentum"] = None if momentum is None else round(momentum, 3)
        else:
            b_norm = math.log1p(r["breadth"]) / math.log1p(max_b) if max_b > 0 else 0.0
            d_norm = math.log1p(r["median_engagement"]) / math.log1p(max_d) if max_d > 0 else 0.0
            r["momentum"] = None
            basis, m_norm = "bootstrap", 0.5

        heat = 100.0 * (w.get("breadth", 0.45) * b_norm
                        + w.get("depth", 0.35) * d_norm
                        + w.get("momentum", 0.20) * m_norm)
        r["b_norm"] = round(b_norm, 3)
        r["d_norm"] = round(d_norm, 3)
        r["m_norm"] = round(m_norm, 3)
        r["momentum_basis"] = basis
        r["heat"] = round(clamp(heat, 0, 100), 1)
        r["caliber"] = "rolling" if use_rolling else "bootstrap"

    ranked = sorted(rows.values(), key=lambda r: (-r["heat"], -r["breadth"]))
    for i, r in enumerate(ranked, 1):
        r["rank"] = i

    caliber = {
        "mode": "rolling" if rolling else "bootstrap",
        "history_days": len(hist_days),
        "min_days_required": min_days,
        "weights": {"breadth": w.get("breadth"), "depth": w.get("depth"), "momentum": w.get("momentum")},
        "note": ("历史 %d 天 ≥ %d，用 90 日滚动分位数归一（广度=去重作者数，深度=单篇互动中位数）"
                 % (len(hist_days), min_days)) if rolling
                else ("历史仅 %d 天（< %d），走冷启动截面归一；动量按中性 0.5 处理，"
                      "热分数值不可与日后 rolling 口径直接比较" % (len(hist_days), min_days)),
    }
    return ranked, caliber


# ---------------------------------------------------------------------------
# 趋势 / 连续上榜
# ---------------------------------------------------------------------------
def annotate_trends(ranked, history, adict, date=None):
    """▲▼─ + 连续在榜天数 + 首次出现日期。

    口径切换日（bootstrap → rolling）不算 delta，标 rebased —— 否则全榜跳变会被误读成行情。
    """
    date = date or today_str()
    hist_days = sorted(history.keys())
    has_history = bool(hist_days)

    for r in ranked:
        aid = r["id"]
        prev_row = None
        for d in reversed(hist_days):
            h = history[d].get(aid)
            if h:
                prev_row = h
                break

        if prev_row is None:
            r["trend"] = "new" if has_history else "flat"
            r["delta_heat"] = None
        elif prev_row.get("caliber") and prev_row.get("caliber") != r.get("caliber"):
            r["trend"] = "rebased"
            r["delta_heat"] = None
        else:
            delta = r["heat"] - float(prev_row.get("heat") or 0)
            r["delta_heat"] = round(delta, 1)
            r["trend"] = "up" if delta >= 3 else ("down" if delta <= -3 else "flat")

        streak, i = 1, 1
        while i <= 400:
            if not at(history, aid, date_minus(date, i)):
                break
            streak += 1
            i += 1
        r["streak_days"] = streak

        first_seen = None
        for d in hist_days:
            if history[d].get(aid):
                first_seen = d
                break
        r["first_seen"] = first_seen or date
    return ranked


# ---------------------------------------------------------------------------
# 动量榜（蹿升）：日历基线
# ---------------------------------------------------------------------------
def surge_list(ranked, history, cfg, date=None, top=3):
    date = date or today_str()
    out = []
    for r in ranked:
        vals = []
        for i in (1, 2, 3):
            h = at(history, r["id"], date_minus(date, i))
            if h:
                vals.append(_pick(h, "breadth", "volume"))
        if len(vals) < 2:          # 至少两个日历日才谈得上"增速"
            continue
        base = sum(vals) / len(vals)
        if base < 1:
            continue
        growth = (r["breadth"] - base) / base
        if growth <= 0.05 or r["breadth"] < 3:
            continue
        out.append({
            "id": r["id"], "name": r["name"], "growth": round(growth, 2),
            "breadth": r["breadth"], "heat": r["heat"], "intent": r["intent"],
            "caliber": "%d日基线" % len(vals),
        })
    out.sort(key=lambda x: -x["growth"])
    return out[:top]


# ---------------------------------------------------------------------------
# 新面孔
# ---------------------------------------------------------------------------
def newcomers(ranked, history, cfg, top=5):
    out = []
    hist_days = sorted(history.keys())
    if not hist_days:
        return out
    for r in ranked[:30]:
        if any(history[d].get(r["id"]) for d in hist_days):
            continue
        if r["breadth"] < 2:
            continue
        out.append({"id": r["id"], "name": r["name"], "heat": r["heat"],
                    "breadth": r["breadth"], "rank": r["rank"]})
        if len(out) >= top:
            break
    return out


# ---------------------------------------------------------------------------
# 事件库（严口径）
# ---------------------------------------------------------------------------
def load_events():
    obj = read_json(EVENTS_PATH, default={"version": 1, "events": []})
    if not isinstance(obj, dict):
        obj = {"version": 1, "events": []}
    obj.setdefault("events", [])
    return obj


def update_events(ranked, history, cfg, date=None):
    """三条门槛入库；连续判定按日历，缺一天就算断。"""
    date = date or today_str()
    store = load_events()
    events = store["events"]
    rules = {r["rule"]: r for r in cfg["events"]["entry_rules"]}
    changes = {"created": [], "updated": [], "evicted": 0, "closed": 0, "skipped_rules": []}
    hist_days = sorted(history.keys())

    today_heat = {r["id"]: float(r["heat"]) for r in ranked}
    today_breadth = {r["id"]: float(r["breadth"]) for r in ranked}

    def _heat_on(aid, d):
        if d == date:
            return today_heat.get(aid)
        h = at(history, aid, d)
        return None if h is None else float(h.get("heat") or 0)

    def _consecutive(aid, days, threshold):
        for i in range(days):
            v = _heat_on(aid, date_minus(date, i))
            if v is None or v < threshold:
                return False
        return True

    def _momentum_pct(aid, percentile):
        """当日广度增速在自身历史增速分布中的分位；样本 <20 判不了。"""
        vals = []
        for i in range(1, 91):
            h = at(history, aid, date_minus(date, i))
            if h:
                vals.append(_pick(h, "breadth", "volume"))   # 时间倒序
        if len(vals) < 20:
            return None
        base = vals[2]                      # 3 天前
        today_m = (today_breadth.get(aid, 0) + 1.0) / (base + 1.0) - 1.0
        moms = []
        for i in range(3, len(vals)):
            moms.append((vals[i - 3] + 1.0) / (vals[i] + 1.0) - 1.0)
        return sum(1 for m in moms if m <= today_m) / float(len(moms)) >= percentile

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
            ev = {"asset_id": aid, "name": r["name"], "category": r["category"],
                  "start_date": date, "last_date": date, "span_days": 1,
                  "peak_heat": r["heat"], "peak_date": date,
                  "entry_reasons": reasons, "intent_peak": r["intent"], "status": "open"}
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
    store["version"] = 3
    write_json(EVENTS_PATH, store, indent=None)
    return store, changes
