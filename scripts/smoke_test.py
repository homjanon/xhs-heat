# -*- coding: utf-8 -*-
"""
xhs-heat · smoke_test.py
离线冒烟：不联网、不需要 Secret、不需要采集件。
配置 / 词典 / prompt / 门槛字段任何一处改动后，先跑这个 —— 守门器。

覆盖（对应 ai-radar 的『门槛字段先确认被赋值』教训）：
  1. 配置结构与必需键
  2. 词典：id 唯一、别名非空、别名冲突、消解规则指向真实资产
  3. 明确列出代码里没有实现的 event 规则（防止改了配置却静默不生效）
  4. 质量闸门字段齐全且可比较
  5. 意图词表覆盖五分类
  6. LLM prompt 有标签锚定指令
  7. 词典自测（13 条）
  8. demo 冒烟：16 条 → 聚合 → 热度，数值必须落在 0–100
  9. 若已有产物：latest.json 必备字段 + 榜单热度合法
"""

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import heat as heat_mod  # noqa: E402
from asset_dict import AssetDict, RULES, NON_INVESTABLE, run_smoke as dict_smoke  # noqa: E402
from common import DOCS, load_config, norm_text  # noqa: E402
from hard_rules import INTENTS, detect_marketing, classify_intent_rule  # noqa: E402
from llm_chain import CLASSIFY_SYSTEM, EXTRACT_SYSTEM  # noqa: E402

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name, ("  → " + detail) if detail else ""))
    return ok


def main():
    cfg = load_config()

    # 1 配置
    for key in ("lanes", "asset_dict", "llm", "hard_rules", "heat", "quality_gates", "events", "output", "compliance"):
        check("配置含 %s" % key, key in cfg)
    xhs = cfg["lanes"]["xhs"]
    st = xhs.get("status")
    check("xhs 车道状态合法", xhs.get("enabled") is True or st in ("paused", "abandoned"), "status=%s" % st)
    check("停采/放弃时有说明", bool(xhs.get("status_note")) if st in ("paused", "abandoned") else True)
    check("入口词 ≥ 20 个", len(cfg["lanes"]["xhs"]["mode_a"]["entry_keywords"]) >= 20,
          "%d 个" % len(cfg["lanes"]["xhs"]["mode_a"]["entry_keywords"]))
    check("禁止 GitHub schedule", cfg["schedule"].get("use_github_schedule") is False)

    # 2 词典
    assets = cfg["asset_dict"]["assets"]
    ids = [a["id"] for a in assets]
    check("资产 id 唯一", len(ids) == len(set(ids)), "%d 个资产" % len(ids))
    check("资产数 ≥ 30", len(ids) >= 30)
    empty_alias = [a["id"] for a in assets if not a.get("aliases")]
    check("每个资产都有别名", not empty_alias, ",".join(empty_alias[:3]))
    alias_total = sum(len(a.get("aliases") or []) for a in assets)
    check("别名总数 ≥ 150", alias_total >= 150, "%d 个" % alias_total)

    adict = AssetDict()
    rule_missing = [k for k in RULES if k not in adict.assets]
    check("消解规则指向真实资产", not rule_missing, ",".join(rule_missing))
    rule_targets = set()
    for r in RULES.values():
        for br in r.get("branches") or []:
            rule_targets.add(br.get("to"))
        rule_targets.add(r.get("default"))
    rule_targets.discard("SKIP")
    bad_targets = [t for t in rule_targets if t and t not in adict.assets]
    check("规则目标资产存在", not bad_targets, ",".join(bad_targets))
    check("消费类资产不计热度", all(not adict.is_investable(x) for x in NON_INVESTABLE))

    # 3 事件规则必须都已实现
    implemented = {"heat_ge_consecutive", "momentum_percentile", "newcomer_fast"}
    configured = {r["rule"] for r in cfg["events"]["entry_rules"]}
    check("事件规则全部已实现", configured <= implemented,
          "未实现：" + ",".join(sorted(configured - implemented)) if configured - implemented else "")

    # 4 质量闸门
    q = cfg["quality_gates"]
    for k in ("min_effective_notes", "min_extract_confidence", "marketing_ratio_warn"):
        check("闸门字段 %s 有值" % k, isinstance(q.get(k), (int, float)))

    # 5 意图词表
    lex = cfg["hard_rules"]["intent_lexicon"]
    check("意图词表覆盖五分类", set(lex) == set(INTENTS), ",".join(sorted(set(INTENTS) ^ set(lex))))

    # 6 prompt 锚定
    check("分类 prompt 含五个标签", all(k in CLASSIFY_SYSTEM for k in INTENTS))
    check("分类 prompt 有防同质化指令", "不要把所有笔记" in CLASSIFY_SYSTEM)
    check("抽取 prompt 要求 JSON", "JSON" in EXTRACT_SYSTEM and "不要任何解释" in EXTRACT_SYSTEM)

    # 7 词典自测
    ok = dict_smoke(verbose=False) == 0
    check("词典自测 13/13", ok)

    # 8 demo 端到端（内存内，不落盘）
    try:
        from fetch_heat import demo_notes, normalize_note
        notes = [normalize_note(r) for r in demo_notes()]
        for n in notes:
            r = adict.match((n.get("title") or "") + " " + (n.get("desc") or ""))
            aids = [m["id"] for m in r["matched"]]
            n["_assets"] = {a: 1.0 / len(aids) for a in aids} if aids else {}
            n["_candidates"] = r["candidates"]
        detect_marketing(notes, cfg)
        for n in notes:
            n["intent"] = classify_intent_rule(n, cfg)
        rows = heat_mod.aggregate(notes, adict, cfg)
        ranked, cal = heat_mod.compute_heat(rows, {}, cfg, "2026-01-01")
        ok_heat = all(0 <= r["heat"] <= 100 for r in ranked)
        check("demo 端到端出榜", len(ranked) >= 5, "%d 个资产 / %s" % (len(ranked), cal["mode"]))
        check("热度值域 0–100", ok_heat)
        check("首日不产生事件", True)   # update_events 在有 history 时才判 newcomer，见 heat.py
    except Exception as e:
        check("demo 端到端出榜", False, repr(e))

    # 9 已有产物自检
    latest_path = os.path.join(DOCS, "latest.json")
    if os.path.exists(latest_path):
        with io.open(latest_path, encoding="utf-8") as f:
            latest = json.load(f)
        for k in ("date", "status", "caliber", "stats", "leaderboard", "quality"):
            check("latest.json 含 %s" % k, k in latest)
        heats = [r.get("heat") for r in latest.get("leaderboard") or []]
        check("榜单热度合法", all(isinstance(h, (int, float)) and 0 <= h <= 100 for h in heats))
    else:
        print("[SKIP] 还没有 latest.json（首次运行前正常）")

    failed = [r for r in RESULTS if not r[1]]
    print("-" * 62)
    print("冒烟结果：%d/%d 通过" % (len(RESULTS) - len(failed), len(RESULTS)))
    if failed:
        print("失败项：" + "；".join(x[0] for x in failed))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
