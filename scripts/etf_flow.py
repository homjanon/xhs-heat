# -*- coding: utf-8 -*-
"""
xhs-heat · etf_flow.py
ETF 资金流：把「概念热度」落到真金白银上

数据源：腾讯行情 qt.gtimg.cn（批量，一次 ≤30 只，GBK）
  实测字段：f[1] 名称 / f[2] 代码 / f[3] 现价 / f[32] 涨跌% / f[37] 成交额(万元) / f[38] 换手率
  优点：不登录、不限流（与东财 push2 不同的风控面）、一次请求覆盖全部自选

量比：与自身近 5 个交易日均值比（历史靠每日快照累积，≥3 天才给，否则留空）

代码来源：从 asset_dict 里取 6 位 ETF 代码（个股代码不会是 5/1 开头 6 位且带 .OF/.SH 后缀），
优先用资产 id 末尾的代码（如 SEMI_512480 → 512480），否则用别名里的代码。
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import DATA_DIR, decode, http_get, log, now_cn, read_json, today_str, write_json

HIST_DIR = os.path.join(DATA_DIR, "etf")
QUOTE = "https://qt.gtimg.cn/q="


def etf_codes(adict):
    """{code: asset_id}，只取 ETF（5/1 开头 6 位）。"""
    out = {}
    for aid, a in adict.assets.items():
        m = re.search(r"(\d{6})$", aid)
        if m and m.group(1)[0] in "15":
            out[m.group(1)] = aid
            continue
        for alias in a.get("aliases") or []:
            alias = str(alias).strip()
            if re.fullmatch(r"\d{6}", alias) and alias[0] in "15":
                out[alias] = aid
                break
    return out


def _prefix(code):
    return "sh" if code[0] == "5" else "sz"


def fetch_quotes(codes, timeout=12):
    """批量取行情，返回 {code: {name, price, pct, amount_yi, turnover}}"""
    out = {}
    for i in range(0, len(codes), 30):
        chunk = codes[i:i + 30]
        url = QUOTE + ",".join(_prefix(c) + c for c in chunk)
        code, body = http_get(url, timeout=timeout, retries=1,
                              headers={"Referer": "https://gu.qq.com/"})
        if code != 200:
            log("  ⚠️ 腾讯行情 HTTP %s（第 %d 批）" % (code, i // 30 + 1))
            continue
        for line in decode(body).split(";"):
            m = re.search(r'v_(\w+)="([^"]*)"', line)
            if not m:
                continue
            f = m.group(2).split("~")
            if len(f) < 39:
                continue
            try:
                out[f[2]] = {
                    "name": f[1],
                    "price": float(f[3] or 0),
                    "pct": float(f[32] or 0),
                    "amount_yi": round(float(f[37] or 0) / 10000.0, 2),   # 万元 → 亿元
                    "turnover": float(f[38] or 0),
                }
            except (ValueError, IndexError):
                continue
    return out


def _history(codes, date, days=5):
    """取 code 的近 days 日成交额均值（不含今天）。"""
    series = {c: [] for c in codes}
    if not os.path.isdir(HIST_DIR):
        return series
    files = sorted(f for f in os.listdir(HIST_DIR) if f.endswith(".json"))
    files = [f for f in files if f[:-5] < date][-days:]
    for f in files:
        obj = read_json(os.path.join(HIST_DIR, f)) or {}
        for c, v in (obj.get("quotes") or {}).items():
            if c in series and v.get("amount_yi"):
                series[c].append(float(v["amount_yi"]))
    return series


def build(cfg, adict, date=None, top=12):
    date = date or today_str()
    mapping = etf_codes(adict)
    if not mapping:
        return {"ok": False, "note": "词典里没有 ETF 代码", "rows": []}

    quotes = fetch_quotes(list(mapping.keys()))
    if not quotes:
        return {"ok": False, "note": "行情接口无返回", "rows": []}

    hist = _history(list(quotes.keys()), date)
    rows = []
    for code, q in quotes.items():
        aid = mapping.get(code, "")
        past = hist.get(code) or []
        ratio = None
        if len(past) >= 3:
            avg = sum(past) / len(past)
            if avg > 0:
                ratio = round(q["amount_yi"] / avg, 2)
        rows.append({
            "code": code,
            "name": q["name"],
            "asset": adict.name_of(aid) if aid else "",
            "asset_id": aid,
            "pct": round(q["pct"], 2),
            "amount_yi": q["amount_yi"],
            "turnover": round(q["turnover"], 2),
            "vol_ratio": ratio,
            "hist_days": len(past),
        })
    rows.sort(key=lambda x: -x["amount_yi"])

    write_json(os.path.join(HIST_DIR, "%s.json" % date),
               {"date": date, "quotes": {r["code"]: {"amount_yi": r["amount_yi"], "pct": r["pct"]}
                                         for r in rows},
                "fetched_at": now_cn().isoformat(timespec="seconds")}, indent=None)

    total = sum(r["amount_yi"] for r in rows)
    log("  ✅ ETF 资金流：%d 只 · 合计 %.0f 亿 · 量比可用 %d 只"
        % (len(rows), total, sum(1 for r in rows if r["vol_ratio"] is not None)))
    return {
        "ok": True,
        "rows": rows[:top],
        "count": len(rows),
        "total_yi": round(total, 1),
        "note": "" if any(r["vol_ratio"] is not None for r in rows)
                else "量比需 ≥3 个交易日快照，今日起累积",
    }


if __name__ == "__main__":
    from asset_dict import AssetDict
    from common import load_config

    res = build(load_config(), AssetDict())
    print(json.dumps(res, ensure_ascii=False, indent=2)[:1500])
