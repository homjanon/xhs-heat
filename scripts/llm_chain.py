# -*- coding: utf-8 -*-
"""
xhs-heat · llm_chain.py
LLM 三档降级链 + 熔断（ai-radar 机制移植，端点复用 portfolio 的 call_llm.py）

链路：Agnes agnes-3.0-flash → Gemini 3.5 Flash-Lite → 商汤 deepseek-flash
纪律：
  - 429 → 本轮熔断该档，不再撞；403/404/410（模型下线/无权限）→ 永久跳过
  - 5xx / 超时 / 网络错 → 换下一档，下批重试
  - 全部档位失败 → 抛 Degraded，上层切词典口径，绝不空窗
  - 单批失败只丢该批，不中断整批（由调用方 try/except）
"""

import json
import os
import re
import time

from common import decode, http_post_json, log, truncate

PERMANENT_CODES = (403, 404, 410)


class Degraded(Exception):
    """所有模型档位都不可用。"""


class LLMChain:
    def __init__(self, cfg, chain="extract"):
        self.cfg = cfg
        self.limits = cfg["llm"]["limits"]
        models = cfg["llm"]["chains"].get(chain, {}).get("models") or []
        if isinstance(models, str) or not models:
            models = cfg["llm"]["chains"]["extract"]["models"]
        self.backends = []
        for m in models:
            key = os.environ.get(m.get("api_key_env", ""), "").strip()
            self.backends.append({
                "name": m.get("name", m.get("model", "?")),
                "api_url": m.get("api_url"),
                "model": m.get("model"),
                "key": key,
                "thinking_off": bool(m.get("thinking_off")),
                "reasoning_low": bool(m.get("reasoning_effort_low")),
                "blocked": not key,        # 无 Key 直接视为不可用
                "blocked_reason": "" if key else "未配置 %s" % m.get("api_key_env"),
                "cool_until": 0.0,        # 429 只冷却一阵，不是永久拉黑
            })
        self.calls = 0
        self.errors = []
        self.used = []
        self.cooldown_s = int((cfg["llm"].get("limits") or {}).get("rate_limit_cooldown_s", 60))

    # ------------------------------------------------------------------
    @property
    def available(self):
        now = time.time()
        return any(not b["blocked"] and b.get("cool_until", 0) <= now for b in self.backends)

    def status(self):
        now = time.time()
        return [{"name": b["name"],
                 "usable": not b["blocked"] and b.get("cool_until", 0) <= now,
                 "reason": b["blocked_reason"] or ("冷却中" if b.get("cool_until", 0) > now else "")}
                for b in self.backends]

    # ------------------------------------------------------------------
    def chat(self, system, user, max_tokens=4000):
        """按链逐档降级调用，返回 (text, backend_name)。全失败抛 Degraded。"""
        if not self.available:
            raise Degraded("无可用模型（未配置任何 API Key）")
        last_err = ""
        now = time.time()
        for b in self.backends:
            if b["blocked"]:
                continue
            if b.get("cool_until", 0) > now:
                last_err = "%s 冷却中（429）" % b["name"]
                continue
            try:
                text = self._call_one(b, system, user, max_tokens)
                if text and text.strip():
                    self.calls += 1
                    if b["name"] not in self.used:
                        self.used.append(b["name"])
                    return text, b["name"]
                last_err = "%s 返回空" % b["name"]
            except _Permanent as e:
                b["blocked"] = True
                b["blocked_reason"] = str(e)
                last_err = str(e)
                log("    🚫 %s" % e)
            except _RateLimited as e:
                # 429 是 RPM 限流：冷却 60s 后允许重试，不整轮拉黑
                b["cool_until"] = time.time() + self.cooldown_s
                b["blocked_reason"] = "429 冷却 %ds" % self.cooldown_s
                last_err = str(e)
                log("    ⛔ %s 429，冷却 %ds（后续批次可重试）" % (b["name"], self.cooldown_s))
            except Exception as e:                      # 瞬时错误：换下一档
                last_err = "%s: %s" % (b["name"], e)
                log("    ⚠️ %s" % truncate(last_err, 120))
        self.errors.append(last_err)
        raise Degraded(last_err or "全部模型失败")

    # ------------------------------------------------------------------
    def _call_one(self, b, system, user, max_tokens):
        payload = {
            "model": b["model"],
            "temperature": 0.2,
            "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        if b["thinking_off"]:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if b["reasoning_low"]:
            payload["reasoning_effort"] = "low"

        timeout = int(self.limits.get("timeout_s", 75))
        code, body = http_post_json(
            b["api_url"], payload, timeout=timeout,
            headers={"Authorization": "Bearer %s" % b["key"]})
        text = decode(body)

        if code == 200:
            try:
                data = json.loads(text)
                msg = (data["choices"][0].get("message") or {})
                out = msg.get("content") or msg.get("reasoning_content") or msg.get("reasoning") or ""
                return out
            except (ValueError, KeyError, IndexError) as e:
                raise RuntimeError("响应解析失败: %s | %s" % (e, truncate(text, 160)))
        if code in PERMANENT_CODES:
            raise _Permanent("%s 不可用 HTTP %s（不重试）: %s"
                             % (b["name"], code, truncate(text, 120)))
        if code == 429:
            raise _RateLimited("%s HTTP 429" % b["name"])
        raise RuntimeError("HTTP %s: %s" % (code, truncate(text, 200)))


class _Permanent(Exception):
    pass


class _RateLimited(Exception):
    pass


# ---------------------------------------------------------------------------
# JSON 解析（模型常带 ``` 围栏或前后废话）
# ---------------------------------------------------------------------------
def parse_json(text, expect="array"):
    if not text:
        return None
    s = text.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    open_ch, close_ch = ("[", "]") if expect == "array" else ("{", "}")
    i, j = s.find(open_ch), s.rfind(close_ch)
    if i < 0 or j <= i:
        return None
    try:
        return json.loads(s[i:j + 1])
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 两个业务调用
# ---------------------------------------------------------------------------
EXTRACT_SYSTEM = (
    "你是投资文本的实体抽取器。任务：从给定的小红书笔记片段中，找出被讨论的【投资资产】。\n"
    "规则：\n"
    "1. 只输出 JSON 数组，不要任何解释、不要 markdown 围栏。\n"
    "2. 每个元素形如 {\"i\": 序号, \"assets\": [\"资产名\"]}；没有资产就给空数组。\n"
    "3. 资产名用最通用的中文叫法（如 黄金ETF、纳指QDII、半导体、存款、比特币、英伟达）。\n"
    "4. 不要输出个股代码以外的臆造标的；不确定就不输出。\n"
    "5. 金饰、三金、婚嫁首饰属于消费，不是投资资产，不要输出。\n"
    "6. 如果片段只是生活抱怨、没有具体资产，输出空数组 —— 这是正常结果，不要硬凑。"
)


def extract_entities(chain, snippets):
    """snippets: [str]；返回 {index: [asset names]}"""
    lines = []
    for i, s in enumerate(snippets):
        lines.append("%d. %s" % (i + 1, truncate(s, 200)))
    user = "请抽取下列 %d 条片段的投资资产：\n%s" % (len(snippets), "\n".join(lines))
    text, _model = chain.chat(EXTRACT_SYSTEM, user, max_tokens=2000)
    data = parse_json(text, "array") or []
    out = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i")) - 1
        except (TypeError, ValueError):
            continue
        assets = item.get("assets") or []
        if isinstance(assets, str):
            assets = [assets]
        out[idx] = [a for a in assets if isinstance(a, str) and a.strip()]
    return out


CLASSIFY_SYSTEM = (
    "你是小红书投资笔记的意图分类器。把每条笔记分到且只分到下面一类：\n"
    "学 = 科普 / 知识 / 产品对比 / 配置原理，没有买卖动作。"
    "例：「资产配置里黄金该占多少」「什么是久期」「纳指和标普区别」\n"
    "买 = 问该不该买 / 怎么买，或已买入。"
    "例：「现在还能上车吗」「新手怎么买纳指」「今天又加了一笔」「定投第 30 期」\n"
    "晒 = 展示收益或持仓战绩（正收益、回本、自嘲式晒）。"
    "例：「感谢大A提车」「三个月40%」「终于回本了」\n"
    "愁 = 已出现亏损语境，求解套。必须命中亏损信号才算：跌了/套牢/亏了/割肉/要不要卖/亏太多。"
    "例：「跌了20%要不要割」「套了两年怎么办」\n"
    "广 = 引流 / 招生 / 卖课 / 明显广告。例：「加微领资料」「内部课最后3个名额」\n"
    "边界纪律：\n"
    "1. 只输出 JSON 数组，元素形如 {\"i\": 序号, \"intent\": \"学\"}，不要解释、不要围栏。\n"
    "2. 必须逐条独立判断，不要把所有笔记都归成同一类（这是最常见的错误）。\n"
    "3. 「还能追吗 / 该不该上车 / 要不要加」是【问操作】→ 买，不是愁；愁必须有明确的亏损事实。\n"
    "4. 没有买卖动作也没有亏损事实的科普、对比、感慨 → 学。\n"
    "5. 判不准时按「读者读完会做什么」选：去查资料→学，去开户/下单→买，想晒→晒，想求助→愁。"
)


def classify_intents(chain, notes):
    """notes: [{title, desc}]；返回 {index: label}"""
    lines = []
    for i, n in enumerate(notes):
        text = (n.get("title") or "") + " " + (n.get("desc") or "")
        lines.append("%d. %s" % (i + 1, truncate(text, 160)))
    user = "请分类下列 %d 条笔记：\n%s" % (len(notes), "\n".join(lines))
    text, _model = chain.chat(CLASSIFY_SYSTEM, user, max_tokens=2000)
    data = parse_json(text, "array") or []
    valid = {"学", "买", "晒", "愁", "广"}
    out = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("i")) - 1
        except (TypeError, ValueError):
            continue
        label = str(item.get("intent") or "").strip()
        if label in valid:
            out[idx] = label
    return out
