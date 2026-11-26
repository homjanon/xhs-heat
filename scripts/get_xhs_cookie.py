# -*- coding: utf-8 -*-
"""
xhs-heat · get_xhs_cookie.py
一次性扫码登录，导出小红书 cookie 串（只落本地 .xhs_cookie.txt，绝不打印、绝不入仓）。
用途：A2 实验 —— 把 cookie 灌进 GitHub Secret，验证机房 IP + 登录态能否采集。

用法：
  python scripts/get_xhs_cookie.py            # 弹浏览器，扫码
  python scripts/get_xhs_cookie.py --check    # 只检查已有 cookie 是否还有效

说明：
  - 用持久化浏览器目录 _xhs_profile/，下次运行免扫（除非登录过期）
  - 轮询 web_session cookie，最长等 8 分钟
"""

import argparse
import io
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PROFILE = os.path.join(ROOT, "_xhs_profile")
COOKIE_FILE = os.path.join(ROOT, ".xhs_cookie.txt")

KEY_NAMES = ("web_session", "a1", "webId", "gid", "xsecappid", "acw_tc", "websectiga", "sec_poison_id")

EDGE_PATHS = (
    r"C://Program Files (x86)//Microsoft//Edge//Application//msedge.exe",
    r"C://Program Files//Microsoft//Edge//Application//msedge.exe",
)


def browser_exe():
    """优先用本机 Edge（免下载 Chromium）；找不到再走 Playwright 自带 chromium。"""
    for p in EDGE_PATHS:
        if os.path.exists(p):
            return p
    return None


def check_login(cookie_str):
    """匿名访客也有 web_session，必须用 user/me 确认是真登录。"""
    try:
        sys.path.insert(0, HERE)
        from probe_xhs_cloud import whoami
        return whoami(cookie_str)
    except Exception as e:
        return False, "校验异常：%r" % e


def dump_cookies(ctx):
    cookies = ctx.cookies()
    parts = []
    for c in cookies:
        if "xiaohongshu.com" not in (c.get("domain") or ""):
            continue
        parts.append("%s=%s" % (c["name"], c["value"]))
    return "; ".join(parts), cookies


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查已有 cookie 是否有效")
    ap.add_argument("--timeout", type=int, default=480)
    args = ap.parse_args(argv)

    if args.check:
        if not os.path.exists(COOKIE_FILE):
            print("❌ 还没有 cookie 文件")
            return 1
        raw = io.open(COOKIE_FILE, encoding="utf-8").read().strip()
        names = [p.split("=", 1)[0] for p in raw.split("; ") if "=" in p]
        print("cookie 长度 %d，字段 %d 个：%s" % (len(raw), len(names), ", ".join(names)))
        print("含 web_session:", "web_session" in names)
        return 0

    from playwright.sync_api import sync_playwright

    os.makedirs(PROFILE, exist_ok=True)
    exe = browser_exe()
    print("使用浏览器：%s" % (exe or "Playwright 自带 Chromium"))
    with sync_playwright() as p:
        opts = dict(
            headless=False,
            viewport={"width": 1280, "height": 880},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check"],
        )
        if exe:
            opts["executable_path"] = exe
        ctx = p.chromium.launch_persistent_context(PROFILE, **opts)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("⚠️ 打开页面超时（不影响扫码）：%s" % e)

        print("")
        print("=" * 58)
        print("浏览器已打开。请用小红书 App 扫码登录（没有二维码就点页面右上角「登录」）")
        print("登录成功后本脚本会自动检测并保存 cookie，然后关闭浏览器。")
        print("=" * 58)

        start = time.time()
        last_note = 0
        last_check = 0
        saved = None
        while time.time() - start < args.timeout:
            time.sleep(2)
            raw, cookies = dump_cookies(ctx)
            names = {c["name"] for c in cookies if "xiaohongshu.com" in (c.get("domain") or "")}
            if "web_session" in names and len(raw) > 80 and time.time() - last_check >= 6:
                last_check = time.time()
                ok, who = check_login(raw)
                if ok:
                    saved = raw
                    print("  ✅ 检测到登录态：%s" % who)
                    break
            waited = int(time.time() - start)
            if waited - last_note >= 15:
                last_note = waited
                print("  …等待扫码 %ds（当前已拿到 %d 个 cookie）" % (waited, len(names)))
                try:
                    page.screenshot(path=os.path.join(ROOT, "_xhs_login.png"))
                except Exception:
                    pass

        if not saved:
            print("❌ 超时未检测到登录态")
            ctx.close()
            return 1

        io.open(COOKIE_FILE, "w", encoding="utf-8", newline="\n").write(saved)
        print("")
        print("✅ 登录成功，cookie 已保存到 .xhs_cookie.txt（长度 %d）" % len(saved))
        print("   字段：%s" % ", ".join(sorted({c["name"] for c in ctx.cookies()
                                              if "xiaohongshu.com" in (c.get("domain") or "")})))
        ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
