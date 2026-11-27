# -*- coding: utf-8 -*-
"""
xhs-heat · test_metrics.py
度量层回归测试（离线，不需要采集件）

覆盖 v3 修订的四个点：
  1. 日历对齐：动量取「日历 3 天前」，缺数据时不靠近邻替代
  2. 广度/深度正交：广度=去重作者数，深度=单篇互动中位数
  3. 口径切换日标 rebased，不算 delta_heat
  4. 单条 cap 迭代收敛，实际占比 ≤ 配置值

用法：python scripts/test_metrics.py
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import heat as H  # noqa: E402
from common import load_config  # noqa: E402

FAIL = []


def chk(name, ok, detail=""):
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))
    if not ok:
        FAIL.append(name)


def mk_row(aid, breadth, median_eng, heat, caliber="rolling"):
    return {"id": aid, "name": aid, "category": "T", "volume": breadth, "notes": int(breadth),
            "breadth": breadth, "median_engagement": median_eng, "heat": heat,
            "caliber": caliber, "intent": {"学": 1.0}}


def main():
    cfg = load_config()
    today = "2026-06-30"

    # ---- 1. cap 迭代 ----
    contrib = [("a", 900.0), ("b", 50.0), ("c", 50.0)]
    total, share = H._cap_iterative(contrib, 0.1, 2)
    chk("cap 生效（最大单条占比 ≤10%）", share <= 0.1001, "实际 %.4f，总量 %.1f" % (share, total))

    # ---- 2. 日历对齐 ----
    hist = {}
    for i in range(1, 41):
        d = H.date_minus(today, i)
        # 故意挖掉「3 天前」和「7 天前」两天，模拟断档
        if i in (3, 7):
            continue
        hist[d] = {"A": mk_row("A", 10 + (i % 3), 100 + i, 50 + i * 0.5)}

    rows = {
        "A": {"id": "A", "name": "A", "category": "T", "volume": 12, "notes": 12,
              "breadth": 12, "median_engagement": 120, "heat": 0, "intent": {"学": 1.0}},
        "B": {"id": "B", "name": "B", "category": "T", "volume": 5, "notes": 5,
              "breadth": 5, "median_engagement": 300, "heat": 0, "intent": {"学": 1.0}},
    }
    ranked, cal = H.compute_heat(rows, hist, cfg, today)
    a = next(r for r in ranked if r["id"] == "A")
    b = next(r for r in ranked if r["id"] == "B")
    chk("历史 38 天 ≥ 30 → rolling", cal["mode"] == "rolling", cal["mode"])
    chk("断档日不靠近邻替代", a["momentum_basis"] == "missing_3d", a["momentum_basis"])
    chk("动量缺失时取中性 0.5", abs(a["m_norm"] - 0.5) < 1e-9, str(a["m_norm"]))
    # 新资产没有自身历史，无法做分位归一 → 该资产单独走截面口径（标 bootstrap），
    # 这是刻意设计：不假装它有历史可比。页面整体口径仍显示 rolling。
    chk("新资产单列为 bootstrap 口径", b["momentum_basis"] == "bootstrap" and b["caliber"] == "bootstrap",
        "%s / %s" % (b["momentum_basis"], b["caliber"]))
    chk("广度/深度分别归一", a["b_norm"] != a["d_norm"], "%.3f / %.3f" % (a["b_norm"], a["d_norm"]))

    # 补齐 3 天前后应能算出动量
    hist[H.date_minus(today, 3)] = {"A": mk_row("A", 4, 100, 50)}
    ranked2, _ = H.compute_heat({k: dict(v) for k, v in rows.items()}, hist, cfg, today)
    a2 = next(r for r in ranked2 if r["id"] == "A")
    chk("3 天前有数据 → calendar_3d", a2["momentum_basis"] == "calendar_3d", a2["momentum_basis"])
    chk("动量方向正确（12 vs 4 → 正）", (a2["momentum"] or 0) > 0, str(a2["momentum"]))

    # ---- 3. rebased ----
    hist2 = dict(hist)
    hist2[H.date_minus(today, 1)] = {"A": mk_row("A", 10, 100, 60, caliber="bootstrap")}
    ranked3, _ = H.compute_heat({k: dict(v) for k, v in rows.items()}, hist2, cfg, today)
    ranked3 = H.annotate_trends(ranked3, hist2, None, today)
    a3 = next(r for r in ranked3 if r["id"] == "A")
    chk("口径切换日标 rebased", a3["trend"] == "rebased", a3["trend"])
    chk("切换日不算 delta", a3["delta_heat"] is None, str(a3["delta_heat"]))

    # ---- 4. 连续在榜按日历断 ----
    streak = a3["streak_days"]
    # 这条用 hist2：1–6 天都在（第 3 天前面补过），第 7 天是缺口 → 应为 7
    chk("连续天数在断档处停止", streak == 7, "streak=%d（第 7 天缺失，应停在 7）" % streak)

    # ---- 5. 蹿升要求 ≥2 个日历日 ----
    surge = H.surge_list([{"id": "B", "name": "B", "breadth": 9, "heat": 50, "intent": {"学": 1}}],
                         {H.date_minus(today, 1): {"B": mk_row("B", 3, 100, 40)}},
                         cfg, today, 3)
    chk("仅 1 个历史日不出蹿升", surge == [], "%d 条" % len(surge))

    print("-" * 62)
    print("度量回归：%d 项，失败 %d 项" % (11, len(FAIL)))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
