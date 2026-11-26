# -*- coding: utf-8 -*-
"""
xhs-heat · probe_sources.py
只读健康探测：不消耗 LLM、不写产物、不发通知。手动或每月自动跑。

探测项：
  1. 采集端：MediaCrawler 目录 / 依赖 / 登录态缓存（本地才有意义）
  2. inbox：最近 7 天采集件是否齐、条数是否异常
  3. 参考车道：RSSHub 实例池逐个 / 东财人气榜
  4. LLM：各档 Key 是否配置（--llm 可加一次真实 1 句调用）
  5. 产物：latest.json 的日期与状态（是否已 stale）

输出：控制台 + GitHub Step Summary（若在 Actions 里）
退出码永远 0（探测失败不该让 CI 变红），除非 --strict。
"""

import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import (DOCS, INBOX_DIR, ROOT, decode, http_get, http_post_json,  # noqa: E402
                    load_config, load_secrets, log, now_cn, read_json, read_jsonl)
from llm_chain import Degraded, LLMChain  # noqa: E402

ROWS = []


def rec(area, item, ok, detail=""):
    ROWS.append({"area": area, "item": item, "ok": bool(ok), "detail": detail})
    log("  [%s] %-28s %s" % ("✓" if ok else "✗", item, detail))


def probe_collector(cfg):
    log("1) 采集端（本地 MediaCrawler）")
    ccfg = cfg["lanes"]["xhs"].get("collector") or {}
    mc_dir = os.path.abspath(os.path.join(HERE, ccfg.get("mc_dir", "../_mc")))
    exists = os.path.isdir(os.path.join(mc_dir, "media_platform", "xhs"))
    rec("采集", "MediaCrawler 目录", exists, mc_dir)
    if not exists:
        rec("采集", "登录态缓存", False, "未安装，跳过")
        return
    login_dir = os.path.join(mc_dir, "browser_data", ccfg.get("login_state_dir", "xhs_user_data_dir"))
    has_login = os.path.isdir(login_dir) and bool(os.listdir(login_dir))
    rec("采集", "登录态缓存", has_login,
        login_dir if has_login else "未找到，首次运行需扫码")
    data_dir = os.path.join(mc_dir, "data", "xhs", "jsonl")
    if os.path.isdir(data_dir):
        files = sorted(os.listdir(data_dir))[-1:]
        rec("采集", "最近落盘文件", bool(files), files[0] if files else "无")
    else:
        rec("采集", "最近落盘文件", False, "data/xhs/jsonl 不存在（还没抓过）")


def probe_inbox(cfg):
    log("2) inbox 采集件（近 7 天）")
    min_notes = int(cfg["quality_gates"]["min_effective_notes"])
    today = now_cn()
    for i in range(7):
        d = (today.replace(hour=0, minute=0, second=0, microsecond=0)).timestamp() - i * 86400
        day = time.strftime("%Y-%m-%d", time.localtime(d))
        p = os.path.join(INBOX_DIR, day + ".jsonl.gz")
        p2 = os.path.join(INBOX_DIR, day + ".jsonl")
        path = p if os.path.exists(p) else (p2 if os.path.exists(p2) else None)
        if not path:
            rec("inbox", day, False, "缺失")
            continue
        n = len(read_jsonl(path))
        rec("inbox", day, n >= min_notes, "%d 条%s" % (n, "" if n >= min_notes else "（低于闸门 %d）" % min_notes))


def probe_reference(cfg):
    log("3) 参考车道")
    lane = cfg["lanes"]["reference"]
    ok_any = False
    for base in lane["rsshub_instances"]:
        t0 = time.time()
        code, body = http_get(base.rstrip("/") + "/xueqiu/hots", timeout=12, retries=0)
        ms = int((time.time() - t0) * 1000)
        items = decode(body).count("<item>") if code == 200 else 0
        ok = code == 200 and items > 0
        ok_any = ok_any or ok
        rec("参考", base.replace("https://", ""), ok, "HTTP %s · %d 条 · %dms" % (code, items, ms))
        if ok:
            break

    src = next((s for s in lane["sources"] if s["id"] == "em_popularity"), None)
    if src:
        payload = dict(src.get("payload") or {})
        code, body = http_post_json(src["url"], payload, timeout=12)
        n = 0
        if code == 200:
            try:
                n = len(json_loads(body).get("data") or [])
            except Exception:
                n = 0
        rec("参考", "东财人气榜", code == 200 and n > 0, "HTTP %s · %d 行" % (code, n))
    return ok_any


def json_loads(b):
    import json
    return json.loads(decode(b))


def probe_llm(cfg, live=False):
    log("4) LLM 链")
    chain = LLMChain(cfg, "extract")
    for b in chain.status():
        rec("LLM", b["name"], b["usable"], b["reason"] or "已配置 Key")
    if live and chain.available:
        try:
            t0 = time.time()
            text, used = chain.chat("你只回答一个词。", "回复：OK", max_tokens=32)
            rec("LLM", "真实调用", True, "%s · %.1fs · %r" % (used, time.time() - t0, (text or "")[:20]))
        except Degraded as e:
            rec("LLM", "真实调用", False, str(e)[:100])


def probe_outputs():
    log("5) 产物")
    latest = read_json(os.path.join(DOCS, "latest.json"))
    if not latest:
        rec("产物", "latest.json", False, "尚未生成")
        return
    age = "?"
    try:
        from datetime import datetime
        gen = datetime.fromisoformat(latest["generated_at"])
        age = "%.1f 小时前" % ((now_cn() - gen).total_seconds() / 3600.0)
    except Exception:
        pass
    rec("产物", "latest.json", True, "%s · %s · %s" % (latest.get("date"), latest.get("status"), age))
    if latest.get("stale"):
        rec("产物", "水印", False, latest.get("stale_note", "")[:80])
    events = read_json(os.path.join(DOCS, "data", "events.json"))
    if events:
        rec("产物", "事件库", True, "%d 条" % len(events.get("events") or []))


def write_summary():
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        lines = ["## xhs-heat 源探测", "", "| 区域 | 项目 | 结果 | 详情 |", "|---|---|---|---|"]
        for r in ROWS:
            lines.append("| %s | %s | %s | %s |" % (r["area"], r["item"], "✅" if r["ok"] else "⚠️", r["detail"]))
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        log("  ⚠️ Step Summary 写入失败：%s" % e)


def main(argv=None):
    ap = argparse.ArgumentParser(description="xhs-heat 源探测（只读）")
    ap.add_argument("--llm", action="store_true", help="做一次真实 LLM 调用（消耗额度）")
    ap.add_argument("--strict", action="store_true", help="有失败项则退出码 1")
    args = ap.parse_args(argv)
    load_secrets()
    cfg = load_config()
    log("=" * 62)
    log("xhs-heat 源探测 · %s" % now_cn().isoformat(timespec="seconds"))
    log("=" * 62)
    probe_collector(cfg)
    probe_inbox(cfg)
    probe_reference(cfg)
    probe_llm(cfg, args.llm)
    probe_outputs()
    write_summary()
    bad = [r for r in ROWS if not r["ok"]]
    print("-" * 62)
    print("探测完成：%d 项，%d 项异常" % (len(ROWS), len(bad)))
    return 1 if (args.strict and bad) else 0


if __name__ == "__main__":
    sys.exit(main())
