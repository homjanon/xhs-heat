# -*- coding: utf-8 -*-
"""
xhs-heat · collect_xhs.py
本地采集器：驱动 MediaCrawler 抓小红书 → 归一化成 inbox/{date}.jsonl.gz

为什么采集必须在本地：
  小红书按 IP 风控，Azure/AWS 段基本必挂；登录态需扫码，CI 无法完成。
  云端（GitHub Actions）只负责处理与发布，见 .github/workflows/publish.yml。

用法：
  python scripts/collect_xhs.py --dry-run                # 只打印命令，不抓
  python scripts/collect_xhs.py --keywords 基金,黄金      # 小样本验证
  python scripts/collect_xhs.py                          # 入口词池全量
  python scripts/collect_xhs.py --push                    # 抓完推到 GitHub 触发发布

首次运行会打开浏览器出二维码，用小红书 App 扫码；登录态缓存在
  _mc/browser_data/xhs_user_data_dir
之后同一台机器免扫（除非过期）。

合规：只读公开笔记，不做任何互动；单词 ≤3 页、串行、间隔 2s。
"""

import argparse
import base64
import glob
import io
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import INBOX_DIR, load_config, log, now_cn, read_jsonl, sha1_short, today_str, write_jsonl_gz  # noqa: E402


def python_cmd(mc_dir):
    """优先用 MediaCrawler 自己的 venv（uv 装 jieba 在本机会被安全钩子拦掉，pip 方案更稳）。"""
    for rel in (os.path.join(".venv", "Scripts", "python.exe"),
                os.path.join(".venv", "bin", "python"),
                os.path.join("venv", "Scripts", "python.exe")):
        p = os.path.join(mc_dir, rel)
        if os.path.exists(p):
            return [p]
    return ["uv", "run", "python"]


def build_cmd(mc_dir, keywords, max_notes, login_type, headless, comments, start_page):
    return python_cmd(mc_dir) + [
        "main.py",
        "--platform", "xhs",
        "--lt", login_type,
        "--type", "search",
        "--keywords", ",".join(keywords),
        "--save_data_option", "jsonl",
        "--crawler_max_notes_count", str(max_notes),
        "--start_page", str(start_page),
        "--get_comments", "yes" if comments else "no",
        "--headless", "yes" if headless else "no",
    ]


def collect_outputs(mc_dir, since_ts=None):
    """读取 MediaCrawler 本次落盘的 jsonl（contents）。"""
    pattern = os.path.join(mc_dir, "data", "xhs", "jsonl", "search_contents_*.jsonl")
    files = sorted(glob.glob(pattern), key=os.path.getmtime)
    rows, used = [], []
    for f in files:
        if since_ts and os.path.getmtime(f) < since_ts:
            continue
        used.append(os.path.basename(f))
        rows.extend(read_jsonl(f))
    return rows, used


def main(argv=None):
    ap = argparse.ArgumentParser(description="小红书采集器（本地）")
    ap.add_argument("--keywords", help="逗号分隔；缺省用 sources.json 的入口词池")
    ap.add_argument("--max-notes", type=int, default=None, help="每个词最多抓多少条")
    ap.add_argument("--start-page", type=int, default=1)
    ap.add_argument("--date", default=None, help="产物日期，默认今天")
    ap.add_argument("--mc-dir", default=None, help="MediaCrawler 目录")
    ap.add_argument("--login-type", default="qrcode", choices=["qrcode", "phone", "cookie"])
    ap.add_argument("--headless", action="store_true", help="无头（首次扫码不要开）")
    ap.add_argument("--no-comments", action="store_true", help="不抓评论（更快）")
    ap.add_argument("--append", action="store_true", help="并入当日已有采集件而不是覆盖")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--push", action="store_true", help="推送到 GitHub 触发发布")
    ap.add_argument("--repo", default="homjanon/xhs-heat")
    args = ap.parse_args(argv)

    cfg = load_config()
    lane = cfg["lanes"]["xhs"]
    ccfg = lane.get("collector") or {}
    mc_dir = os.path.abspath(args.mc_dir or os.path.join(HERE, ccfg.get("mc_dir", "../_mc")))
    date = args.date or today_str()

    if args.keywords:
        keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    else:
        keywords = lane["mode_a"]["entry_keywords"]
    max_notes = args.max_notes or int(ccfg.get("max_notes_per_keyword", 20))

    if not os.path.isdir(mc_dir):
        log("❌ 找不到 MediaCrawler：%s" % mc_dir)
        log("   git clone https://github.com/NanmiCoder/MediaCrawler.git %s && cd %s && uv sync" % (mc_dir, mc_dir))
        return 1

    cmd = build_cmd(mc_dir, keywords, max_notes, args.login_type,
                    args.headless, not args.no_comments, args.start_page)
    log("媒体采集 · %d 个关键词 · 每词 %d 条 · 输出 %s" % (len(keywords), max_notes, date))
    log("命令：%s" % " ".join(cmd))
    if args.dry_run:
        return 0

    t0 = now_cn().timestamp()
    log("")
    log("▶️ 开始采集（首次运行会弹出浏览器二维码，请用小红书 App 扫码）…")
    try:
        proc = subprocess.run(cmd, cwd=mc_dir)
    except FileNotFoundError:
        log("❌ 未找到 uv。先装：pip install uv")
        return 1
    if proc.returncode != 0:
        log("⚠️ MediaCrawler 退出码 %d（风控/登录态过期时常见）" % proc.returncode)

    rows, used = collect_outputs(mc_dir, since_ts=t0 - 5)
    if not rows:
        if used:
            log("❌ 本次没有落盘数据（命中的文件：%s）" % "、".join(used))
        else:
            log("❌ 没有新的 jsonl 文件")
        log("   排查：① 是否扫码成功 ② 是否触发风控（返回空/403/验证码 → 停 24h 再试）")
        return 1

    # 归一化 + 去重（保留原始字段，下游 normalize_note 自己取）
    seen, out = set(), []
    for r in rows:
        nid = str(r.get("note_id") or r.get("id") or "")
        key = nid or sha1_short((r.get("title") or "") + (r.get("desc") or ""), 16)
        if key in seen:
            continue
        seen.add(key)
        if not r.get("source_keyword"):
            r["source_keyword"] = r.get("keyword") or ""
        r["_collected_at"] = now_cn().isoformat(timespec="seconds")
        out.append(r)

    path = os.path.join(INBOX_DIR, "%s.jsonl.gz" % date)
    if args.append and os.path.exists(path):
        old = read_jsonl(path)
        old_keys = {str(x.get("note_id") or x.get("id") or "") for x in old}
        merged = old + [r for r in out if str(r.get("note_id") or r.get("id") or "") not in old_keys]
        out = merged

    size = write_jsonl_gz(path, out)
    log("")
    log("✅ 采集完成：%d 条（去重后）→ inbox/%s.jsonl.gz（%.1f KB）" % (len(out), date, size / 1024.0))
    log("   来源文件：%s" % "、".join(used))

    if args.push:
        return push(path, args.repo)
    log("   下一步：python scripts/fetch_heat.py   或加 --push 直接推 GitHub")
    return 0


def push(path, repo):
    """用 GitHub API 上传（git push 在本地被墙）。需要 gh 已登录。"""
    name = os.path.basename(path)
    with open(path, "rb") as f:
        content = base64.b64encode(f.read()).decode("ascii")
    payload = {
        "message": "inbox: %s" % name,
        "content": content,
        "branch": "main",
    }
    api = "/repos/%s/contents/inbox/%s" % (repo, name)
    log("☁️ 推送到 %s/inbox/%s …" % (repo, name))
    proc = subprocess.run(["gh", "api", "-X", "PUT", api, "--input", "-"],
                          input=json.dumps(payload).encode("utf-8"),
                          capture_output=True)
    if proc.returncode != 0:
        log("❌ 推送失败：%s" % proc.stderr.decode("utf-8", "ignore")[:300])
        return 1
    log("✅ 已推送，workflow 会被 push 触发（inbox/**）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
