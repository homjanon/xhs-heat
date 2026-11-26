# -*- coding: utf-8 -*-
"""
xhs-heat · fetch_xhs.py
云端采集器（GitHub Actions 内运行，无需浏览器）

原理（2026-11-26 实测）：
  - 小红书签名已可由 xhshow 纯算法生成，不需要 Playwright / MediaCrawler
  - 唯一必需的是登录 cookie（Secret: XHS_COOKIE）
  - 匿名访客也有 web_session，所以必须用 /user/me 的 guest 字段判定登录

风控纪律（实测出来的）：
  - page_size 最大 20；sort 只允许 general / time_descending
    （popularity_descending 直接 461）
  - 请求间隔 5–7 秒；连续 2 次 461 立即中止本轮（进入冷却期后硬撞只会延长封禁）
  - 单轮上限 ~25 个关键词，产出 ~400–550 条

退出码：
  0 正常；2 部分成功（有数据但有失败词）；3 登录态失效（cookie 过期）；4 全部 461
"""

import argparse
import io
import json
import os
import random
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import (DATA_DIR, INBOX_DIR, load_config, load_secrets, log,  # noqa: E402
                    now_cn, read_json, read_jsonl, step, today_str, truncate,
                    write_json, write_jsonl_gz)

API_SEARCH = "/api/sns/web/v1/search/notes"
API_ME = "/api/sns/web/v2/user/me"
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

RC_OK, RC_PARTIAL, RC_AUTH, RC_BLOCKED = 0, 2, 3, 4


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
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


def _signed(cookie, uri, payload, method="POST"):
    import requests
    from xhshow import Xhshow

    client = Xhshow()
    if method == "GET":
        signs = client.sign_headers_get(uri=uri, cookies=cookie, params=payload or {})
    else:
        signs = client.sign_headers_post(uri=uri, cookies=cookie, payload=payload or {})
    headers = dict(BASE_HEADERS)
    headers.update({
        "Cookie": cookie,
        "X-S": signs.get("x-s", ""),
        "X-T": signs.get("x-t", ""),
        "x-S-Common": signs.get("x-s-common", ""),
        "X-B3-Traceid": signs.get("x-b3-traceid", ""),
    })
    if method == "GET":
        return requests.get(HOST + uri, params=payload or {}, headers=headers, timeout=25)
    return requests.post(HOST + uri, json=payload, headers=headers, timeout=25)


def whoami(cookie):
    """返回 (ok, 文案)。guest=true 说明只是游客态，不能采集。"""
    try:
        r = _signed(cookie, API_ME, {}, method="GET")
        data = r.json()
    except Exception as e:
        return False, "校验异常：%r" % e
    d = data.get("data") or {}
    if data.get("code") == 0 and d and not d.get("guest", True):
        return True, str(d.get("nickname") or d.get("user_id") or "")
    return False, "code=%s guest=%s" % (data.get("code"), d.get("guest"))


# ---------------------------------------------------------------------------
# 时间解析：只有相对时间（"9小时前"/"昨天"/"11-20"）
# ---------------------------------------------------------------------------
def parse_relative_time(text, now=None):
    import re
    from datetime import timedelta

    now = now or now_cn()
    if not text:
        return None
    t = str(text).strip()
    m = re.match(r"^(\d+)\s*(秒|分钟|小时|天)前$", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"秒": timedelta(seconds=n), "分钟": timedelta(minutes=n),
                 "小时": timedelta(hours=n), "天": timedelta(days=n)}[unit]
        return (now - delta).isoformat(timespec="seconds")
    if t in ("刚刚", "刚才"):
        return now.isoformat(timespec="seconds")
    if t == "昨天":
        return (now - timedelta(days=1)).isoformat(timespec="seconds")
    if t == "前天":
        return (now - timedelta(days=2)).isoformat(timespec="seconds")
    m = re.match(r"^(\d+)\s*天前$", t)
    if m:
        return (now - timedelta(days=int(m.group(1)))).isoformat(timespec="seconds")
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", t)
    if m:
        try:
            dt = now.replace(month=int(m.group(1)), day=int(m.group(2)),
                             hour=12, minute=0, second=0, microsecond=0)
            if dt > now:
                dt = dt.replace(year=dt.year - 1)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            return None
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return "%04d-%02d-%02dT12:00:00+08:00" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _publish_tag(note_card):
    for tag in (note_card.get("corner_tag_info") or []):
        if isinstance(tag, dict) and tag.get("type") in ("publish_time", "publish_time_new"):
            return tag.get("text") or ""
    return ""


# ---------------------------------------------------------------------------
# 单次搜索
# ---------------------------------------------------------------------------
def search_once(cookie, keyword, page=1, size=20, sort="time_descending"):
    payload = {
        "keyword": keyword, "page": page, "page_size": size,
        "search_id": new_search_id(), "sort": sort, "note_type": 0,
    }
    r = _signed(cookie, API_SEARCH, payload)
    status = r.status_code
    try:
        data = r.json()
    except ValueError:
        return status, None, []
    return status, data.get("code"), ((data.get("data") or {}).get("items") or [])


def to_record(item, keyword):
    nc = item.get("note_card") or {}
    user = nc.get("user") or {}
    inter = nc.get("interact_info") or {}
    tag = _publish_tag(nc)
    return {
        "note_id": item.get("id") or "",
        "title": (nc.get("display_title") or "").strip(),
        "desc": "",                      # 搜索接口不返回正文；需要时再走详情接口
        "liked_count": inter.get("liked_count") or 0,
        "collected_count": inter.get("collected_count") or 0,
        "comment_count": inter.get("comment_count") or 0,
        "share_count": inter.get("shared_count") or 0,
        "time": parse_relative_time(tag),
        "publish_hint": tag,             # 原文，便于回溯口径
        "author_hash": None,             # 下游 normalize_note 用 user_id 哈希，这里只留空
        "user_id": user.get("user_id") or "",
        "source_keyword": keyword,
        "note_type": nc.get("type") or "",
        "note_url": ("https://www.xiaohongshu.com/explore/%s" % item.get("id")) if item.get("id") else "",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def rotate_keywords(pool, n, seed):
    """按日期确定性轮换：同一天重跑结果一致，几天下来覆盖整个词池。

    为什么轮换而不是全跑：461 是账号级风控，实测连续 12–14 次请求后触发，
    单位是「每天能发多少次」而不是「能抓多少词」。
    """
    import hashlib

    if n >= len(pool):
        return list(pool), False
    h = int(hashlib.sha1(str(seed).encode()).hexdigest()[:8], 16)
    start = h % len(pool)
    picked = [pool[(start + i) % len(pool)] for i in range(n)]
    return picked, True


def run(args):
    load_secrets()
    cfg = load_config()
    date = args.date or today_str()

    cookie = (os.environ.get("XHS_COOKIE") or "").strip()
    if not cookie:
        log("❌ 未配置 XHS_COOKIE（GitHub Secret 或本地 .env.local）")
        return RC_AUTH
    ok, who = whoami(cookie)
    if not ok:
        log("❌ 登录态无效：%s" % who)
        log("   → 本地跑 python scripts/get_xhs_cookie.py 重新扫码，再更新 Secret XHS_COOKIE")
        return RC_AUTH
    log("✅ 登录态有效：%s" % who)

    keywords = [k.strip() for k in (args.keywords or "").split(",") if k.strip()]
    rotated = False
    if not keywords:
        pool = cfg["lanes"]["xhs"]["mode_a"]["entry_keywords"]
        daily = int(cfg["lanes"]["xhs"]["mode_a"].get("daily_keyword_limit", 15))
        if args.limit:
            daily = args.limit
        keywords, rotated = rotate_keywords(pool, daily, date)
        log("词池轮换：从 %d 个里取 %d 个（seed=%s），今日不重复、跨日全覆盖"
            % (len(pool), len(keywords), date))
    elif args.limit:
        keywords = keywords[: args.limit]
    log("关键词 %d 个 · 每词 %d 页 · 间隔 %.0f–%.0fs"
        % (len(keywords), args.pages, args.delay, args.delay + 2))

    t0 = time.time()
    seen, records = set(), []
    per_kw, failures = {}, []
    consec_461 = 0
    blocked = False

    for i, kw in enumerate(keywords, 1):
        got = 0
        for page in range(1, args.pages + 1):
            try:
                status, code, items = search_once(cookie, kw, page, args.page_size, args.sort)
            except Exception as e:
                failures.append({"keyword": kw, "page": page, "reason": "异常:%s" % truncate(str(e), 60)})
                break

            if status == 461 or code in (461, 300011, 300012):
                consec_461 += 1
                log("   ⚠️ %s p%d → HTTP %s code=%s（连续 461：%d）" % (kw, page, status, code, consec_461))
                failures.append({"keyword": kw, "page": page, "reason": "461/风控", "status": status})
                if consec_461 >= args.max_461:
                    log("   ⛔ 连续 %d 次风控，本轮中止（已保留抓到的数据）" % consec_461)
                    blocked = True
                    break
                time.sleep(args.backoff)
                # 退避后重试一次
                try:
                    status, code, items = search_once(cookie, kw, page, args.page_size, args.sort)
                except Exception:
                    break
                if status == 461 or code in (461, 300011, 300012):
                    failures.append({"keyword": kw, "page": page, "reason": "退避后仍 461"})
                    break
            else:
                consec_461 = 0

            if code not in (0, None):
                failures.append({"keyword": kw, "page": page, "reason": "code=%s" % code})
                break

            for it in items:
                rec = to_record(it, kw)
                if not rec["note_id"] or rec["note_id"] in seen:
                    continue
                seen.add(rec["note_id"])
                records.append(rec)
                got += 1
            time.sleep(args.delay + random.uniform(0, 2))
        per_kw[kw] = got
        log("   [%2d/%d] %-8s %3d 条%s" % (i, len(keywords), kw, got,
                                          "  （0 条：可能该词当期无内容）" if got == 0 else ""))
        if blocked:
            break

    log("")
    log("抓取结束：%d 条唯一笔记 · 耗时 %.0fs · 失败词 %d 个"
        % (len(records), time.time() - t0, len(failures)))

    # 写采集件（与本地 MediaCrawler 输出同构，下游 normalize_note 直接吃）
    path = os.path.join(INBOX_DIR, "%s.jsonl.gz" % date)
    if args.append and os.path.exists(path):
        old = read_jsonl(path)
        old_ids = {str(x.get("note_id") or "") for x in old}
        records = old + [r for r in records if r["note_id"] not in old_ids]
    if records:
        write_jsonl_gz(path, records)
        log("✅ 已写入 inbox/%s.jsonl.gz（%d 条）" % (date, len(records)))

    ts = now_cn().strftime("%Y%m%dT%H%M%S")
    report = {
        "ts": now_cn().isoformat(timespec="seconds"), "date": date, "kind": "collect",
        "cookie_user": who, "keywords": len(keywords), "pages": args.pages,
        "sort": args.sort, "page_size": args.page_size,
        "notes": len(records), "per_keyword": per_kw, "failures": failures,
        "blocked": blocked, "rotated": rotated,
        "elapsed_s": round(time.time() - t0, 1),
    }
    write_json(os.path.join(DATA_DIR, "reports", "collect_%s.json" % ts), report, indent=None)

    if not records:
        return RC_BLOCKED if blocked else RC_PARTIAL
    return RC_PARTIAL if (failures or blocked) else RC_OK


def main(argv=None):
    ap = argparse.ArgumentParser(description="小红书云端采集器")
    ap.add_argument("--keywords", help="逗号分隔；缺省用配置的入口词池")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个词")
    ap.add_argument("--pages", type=int, default=1)
    ap.add_argument("--page-size", type=int, default=20)
    ap.add_argument("--sort", default="time_descending", choices=["time_descending", "general"])
    ap.add_argument("--delay", type=float, default=5.0, help="请求间隔基数（秒）")
    ap.add_argument("--backoff", type=float, default=90.0, help="461 后等待秒数")
    ap.add_argument("--max-461", type=int, default=2, help="连续多少次 461 中止")
    ap.add_argument("--date", default=None)
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
