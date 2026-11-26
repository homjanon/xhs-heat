# -*- coding: utf-8 -*-
"""
xhs-heat · probe_xhs_cloud.py
A2 实验：只用「cookie + xhshow 纯算法签名」调小红书搜索接口，验证机房 IP 能否采集。

为什么不需要浏览器：
  MediaCrawler 2026 版已把签名换成 xhshow（纯算法，MIT），不再依赖页面 JS。
  所以云端只剩两个变量：① cookie 是否有效 ② IP 是否被风控打分。

用法：
  # 控制组（本机家宽）
  python scripts/probe_xhs_cloud.py --keywords 基金 --show 3
  # 实验组（GitHub Actions）由 workflow 注入 XHS_COOKIE

判定：
  HTTP 200 + code 0 + 有 items  → 云端可行
  code 461 / 300011 / -100      → 分别对应 风控拦截 / 安全限制 / 登录失效
"""

import argparse
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

API = "/api/sns/web/v1/search/notes"
HOST = "https://edith.xiaohongshu.com"
BASE_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "zh-CN,zh;q=0.9",
    "cache-control": "no-cache",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.xiaohongshu.com",
    "pragma": "no-cache",
    "priority": "u=1, i",
    "referer": "https://www.xiaohongshu.com/",
    "sec-ch-ua": '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36"),
}

ERR_HINT = {
    461: "风控拦截（账号/设备/IP 打分）",
    300011: "安全限制（需验证码/二次校验）",
    300012: "网络异常（平台侧要求检查网络，通常是 IP 段被标）",
    -100: "登录态失效（cookie 过期或不是同一设备）",
}


def base36(n):
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if n == 0:
        return "0"
    out = ""
    while n:
        out = digits[n % 36] + out
        n //= 36
    return out


def new_search_id():
    return base36((int(time.time() * 1000) << 64) + int(random.uniform(0, 2147483646)))


def search(cookie, keyword, page=1, size=20):
    import requests
    from xhshow import Xhshow

    payload = {
        "keyword": keyword,
        "page": page,
        "page_size": size,
        "search_id": new_search_id(),
        "sort": "general",
        "note_type": 0,
    }
    signs = Xhshow().sign_headers_post(uri=API, cookies=cookie, payload=payload)
    headers = dict(BASE_HEADERS)
    headers.update({
        "Cookie": cookie,
        "X-S": signs.get("x-s", ""),
        "X-T": signs.get("x-t", ""),
        "x-S-Common": signs.get("x-s-common", ""),
        "X-B3-Traceid": signs.get("x-b3-traceid", ""),
    })
    return requests.post(HOST + API, json=payload, headers=headers, timeout=25)


def whoami(cookie):
    """确认 cookie 是否真的是登录态：/user/me 返回 guest=False 才算。"""
    import requests
    from xhshow import Xhshow
    uri = "/api/sns/web/v2/user/me"
    signs = Xhshow().sign_headers_get(uri=uri, cookies=cookie, params={})
    headers = dict(BASE_HEADERS)
    headers.update({"Cookie": cookie, "X-S": signs.get("x-s", ""), "X-T": signs.get("x-t", ""),
                    "x-S-Common": signs.get("x-s-common", ""),
                    "X-B3-Traceid": signs.get("x-b3-traceid", "")})
    r = requests.get(HOST + uri, headers=headers, timeout=20)
    try:
        data = r.json()
    except ValueError:
        return False, "非 JSON：%s" % r.text[:100]
    d = data.get("data") or {}
    if data.get("code") == 0 and d and not d.get("guest", True):
        return True, "已登录：%s" % (d.get("nickname") or d.get("user_id") or "")
    return False, "未登录（code=%s guest=%s）" % (data.get("code"), d.get("guest"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--keywords", default="基金")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--show", type=int, default=3, help="每条关键词打印几条样本")
    args = ap.parse_args(argv)

    cookie = (os.environ.get("XHS_COOKIE") or "").strip()
    if not cookie:
        print("❌ 没有 XHS_COOKIE 环境变量")
        return 1
    print("cookie 长度 %d，含 web_session: %s" % (len(cookie), "web_session" in cookie))
    ok, msg = whoami(cookie)
    print("登录态检查：%s（%s）" % ("✅" if ok else "❌", msg))
    if not ok:
        return 3

    ok_total = 0
    for kw in [k.strip() for k in args.keywords.split(",") if k.strip()]:
        for page in range(1, args.pages + 1):
            print("")
            print("── 关键词「%s」第 %d 页" % (kw, page))
            try:
                r = search(cookie, kw, page)
            except Exception as e:
                print("   ❌ 请求异常：%r" % e)
                return 1
            print("   HTTP %s · %d bytes" % (r.status_code, len(r.content)))
            try:
                data = r.json()
            except ValueError:
                print("   非 JSON，头部：%s" % r.text[:160].replace("\n", " "))
                continue
            code = data.get("code")
            print("   code=%s msg=%s" % (code, str(data.get("msg"))[:80]))
            if code not in (0, None):
                hint = ERR_HINT.get(code)
                if hint:
                    print("   ↳ %s" % hint)
                continue
            items = ((data.get("data") or {}).get("items") or [])
            ok_total += len(items)
            print("   拿到笔记 %d 条" % len(items))
            for it in items[: args.show]:
                nc = (it.get("note_card") or {})
                user = (nc.get("user") or {}).get("nickname", "")
                inter = (nc.get("interact_info") or {})
                print("     · %s | 赞%s 藏%s 评%s | %s"
                      % ((nc.get("display_title") or "")[:34],
                         inter.get("liked_count", "?"), inter.get("collected_count", "?"),
                         inter.get("comment_count", "?"), user[:12]))
            time.sleep(2)

    print("")
    print("=" * 56)
    print("合计拿到笔记：%d 条" % ok_total)
    print("结论：%s" % ("✅ 该 IP 可采集" if ok_total > 0 else "❌ 该 IP 拿不到数据（见上方 code）"))
    return 0 if ok_total > 0 else 2


if __name__ == "__main__":
    sys.exit(main())
