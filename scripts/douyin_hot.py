# -*- coding: utf-8 -*-
"""
xhs-heat · douyin_hot.py
抖音热榜 → 「出圈事件层」

定位（重要）：**不进热度加权**。
  抖音热榜是稀疏、二值、有时滞的信号：99% 的日子资产不在榜上，值是 0 或 1，
  与 0–100 的连续热度混合会制造巨大跳变；而且"上榜"是已经发生的事实，不是正在积累的趋势。
  正确用法是记事件：`{date, 资产, 名次, 热值, 原文}`，这是唯一有绝对量纲的跨人群指标。

实测（2026-11-27）：GET iesdouyin 200 / 50 条，字段 word / hot_value / label（没有 position，名次取数组下标）。
完全公开、无需登录、零账号风险。
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import DATA_DIR, decode, http_get, log, now_cn, read_json, today_str, write_json

API = "https://www.iesdouyin.com/web/api/v2/hotsearch/billboard/word/"
HIST_DIR = os.path.join(DATA_DIR, "douyin")

# 财经信号词：命中即认为是财经相关（即使没匹配到资产），用于「市场级」事件
FIN_WORDS = [
    "A股", "股", "基金", "债", "利率", "降息", "加息", "降准", "央行", "证监会", "国常会",
    "楼市", "房价", "房贷", "金价", "黄金", "贵金属", "纪念币", "银价", "比特币", "币圈",
    "汇率", "人民币", "美元", "关税", "涨停", "跌停", "熔断", "牛市", "熊市", "CPI", "PPI",
    "GDP", "通胀", "通缩", "降薪", "裁员", "退税", "补贴", "医保", "养老金", "存款",
]


def fetch(limit=50, timeout=15):
    code, body = http_get(API, timeout=timeout, retries=1,
                          headers={"Referer": "https://www.douyin.com/"})
    if code != 200:
        log("  ⚠️ 抖音热榜 HTTP %s" % code)
        return {"ok": False, "items": [], "note": "HTTP %s" % code}
    try:
        data = json.loads(decode(body))
    except ValueError:
        return {"ok": False, "items": [], "note": "响应非 JSON"}
    words = data.get("word_list") or []
    if not words:
        return {"ok": False, "items": [], "note": "空列表"}

    items = []
    for i, w in enumerate(words[:limit], 1):
        items.append({
            "rank": i,
            "word": (w.get("word") or "").strip(),
            "hot_value": int(w.get("hot_value") or 0),
            "label": w.get("label") or "",
        })
    log("  ✅ 抖音热榜：%d 条" % len(items))
    return {"ok": True, "items": items, "note": ""}


def classify(items, adict):
    """分成两类：asset（命中资产）与 market（只命中财经信号词）。"""
    out = []
    for it in items:
        text = it["word"]
        m = adict.match(text)
        aids = [x["id"] for x in m["matched"] if adict.is_investable(x["id"])]
        is_fin = any(k in text for k in FIN_WORDS)
        if not aids and not is_fin:
            continue
        out.append({
            "rank": it["rank"],
            "word": text,
            "hot_value": it["hot_value"],
            "kind": "asset" if aids else "market",
            "assets": [adict.name_of(a) for a in aids],
        })
    return out


def build(cfg, adict, date=None, keep_history=True):
    date = date or today_str()
    raw = fetch()
    if not raw["ok"]:
        return {"ok": False, "items": [], "note": raw.get("note"), "source": "iesdouyin"}

    events = classify(raw["items"], adict)
    if keep_history and events:
        path = os.path.join(HIST_DIR, "%s.json" % date)
        write_json(path, {"date": date, "items": events,
                          "fetched_at": now_cn().isoformat(timespec="seconds")}, indent=None)

    if events:
        log("  ✅ 出圈事件：%d 条（best #%d %s）" % (len(events), events[0]["rank"], events[0]["word"][:16]))
    else:
        log("  ○ 出圈事件：今日热榜无财经/资产条目")

    return {
        "ok": True,
        "source": "iesdouyin",
        "total_board": len(raw["items"]),
        "matched": len(events),
        "items": events,
        "note": "" if events else "今日无财经条目（正常，稀疏信号）",
    }


def recent(days=7, date=None):
    """近 N 天出圈记录（供页面显示"连续第几天在榜"）。"""
    date = date or today_str()
    out = {}
    if not os.path.isdir(HIST_DIR):
        return out
    files = sorted(f for f in os.listdir(HIST_DIR) if f.endswith(".json"))[-days:]
    for f in files:
        obj = read_json(os.path.join(HIST_DIR, f)) or {}
        for it in obj.get("items") or []:
            for name in (it.get("assets") or []):
                out.setdefault(name, []).append({"date": obj.get("date"), "rank": it.get("rank")})
    return out


if __name__ == "__main__":
    from asset_dict import AssetDict
    from common import load_config

    cfg = load_config()
    ad = AssetDict()
    res = build(cfg, ad, keep_history=False)
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1200])
