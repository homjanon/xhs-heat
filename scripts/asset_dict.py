# -*- coding: utf-8 -*-
"""
xhs-heat · asset_dict.py
资产词典匹配 + 歧义消解（纯硬规则层，ai-radar 原则一：能用硬规则就不用 LLM）

职责（v3 设计 2.3 节两级抽取之第一级）：
  1. 对笔记文本做词典匹配（最长别名优先，吃掉 ~70% 高频资产）
  2. 应用消解规则处理歧义词（"黄金"=金饰还是ETF、"算力"=CPO还是英伟达…）
  3. 词典未命中的片段进入候选池，交 LLM 长尾链处理
  4. LLM 全挂时本模块单独即可出"词典模式"榜单 —— 绝不空窗

设计约束：
  - 纯标准库，零第三方依赖
  - 词典全部来自 sources.json，本文件不含资产数据（配置驱动）
  - 规则或词典变更后必须跑：python scripts/asset_dict.py（内置自测）

v5 修复记录（2026-11-26）：
  - 匹配改为「最长别名优先 + 位置掩码」，修掉旧实现里短别名抢位导致长别名被丢弃的问题
  - 消解规则支持 invest_any / else_conf，黄金「无上下文」不再硬判 0.9
  - 新增 resolve() 供 LLM 输出回查词典；新增 is_investable() 供热度层剔除消费类
"""

import io
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from common import clean_text, load_config, norm_text, truncate  # noqa: E402

DEFAULT_DICT_PATH = os.path.join(HERE, "sources.json")

# 候选池停用词（片段级）
STOP_FRAGMENTS = {
    "今天", "明天", "昨天", "现在", "可以", "怎么", "什么", "为什么", "大家",
    "自己", "没有", "有点", "真的", "感觉", "觉得", "开始", "时候", "这个",
    "那个", "一下", "然后", "所以", "但是", "如果", "还是", "已经", "可能",
    "知道", "朋友", "姐妹", "兄弟", "宝宝", "姐妹们", "求推荐", "有没有",
    "讨论", "分享", "记录", "日常", "生活", "上班", "下班", "工资", "月薪",
    "万块", "预算", "小白", "新手", "入门", "老铁", "宝子", "集美", "一个",
    "我们", "他们", "你们", "不是", "就是", "这些", "那些", "很多", "非常",
    "特别", "其实", "当然", "不过", "而且", "或者", "因为", "只要", "只有",
    "第一", "第二", "最后", "最近", "今年", "去年", "明年", "之前",
    "之后", "目前", "绝对", "一定", "建议", "选择", "问题",
    "情况", "方式", "方法", "东西", "事情", "时间", "地方", "市场", "行情",
}

# 消解规则：与 sources.json 里 disambiguation 的文字描述保持同步。
# when_any / invest_any 命中判断都在整篇笔记的规范化文本上做。
# 规则结构（按 branches 顺序命中即止，都不中走 default）：
#   branches: [{"any": [关键词], "to": asset_id 或 "SKIP", "conf": 0.9}]
#   default: asset_id 或 "SKIP"；default_conf
RULES = {
    "GOLD_ETF_518880": {
        "branches": [
            {"any": ["三金", "婚", "首饰", "金镯", "嫁妆", "买金子送", "金项链", "金饰"],
             "to": "GOLD_JEWELRY", "conf": 0.85},
            {"any": ["定投", "净值", "上车", "etf", "存金", "积存金", "仓位", "收益率", "518880", "抄底", "加仓"],
             "to": "GOLD_ETF_518880", "conf": 0.9},
        ],
        "default": "GOLD_ETF_518880",
        "default_conf": 0.6,      # 无任何语境信号：仍归投资，但降置信
    },
    "GOLD_JEWELRY": {
        "branches": [],
        "default": "GOLD_JEWELRY",
        "default_conf": 0.85,
    },
    "AI_ETF": {
        "branches": [
            {"any": ["cpo", "光模块", "光芯片"], "to": "CPO_515880", "conf": 0.9},
            {"any": ["英伟达", "nvidia", "gpu"], "to": "NVDA", "conf": 0.85},
        ],
        "default": "AI_ETF",
        "default_conf": 0.9,
    },
    "BROKER_512880": {
        "branches": [
            {"any": ["开户", "账户", "佣金"], "to": "SKIP", "conf": 1.0},
        ],
        "default": "BROKER_512880",
        "default_conf": 0.9,
    },
    "HOUSE": {
        "branches": [
            {"any": ["投资", "出租", "收租", "租金回报", "租售比"], "to": "HOUSE", "conf": 0.8},
        ],
        "default": "SKIP",
        "default_conf": 1.0,
    },
}

# 消费类资产：不计入投资热度，只在报告中单列
NON_INVESTABLE = {"GOLD_JEWELRY"}


class AssetDict:
    def __init__(self, dict_path=DEFAULT_DICT_PATH):
        self.cfg = load_config(dict_path)
        self.assets = {}
        self.alias_map = {}     # norm_alias -> (asset_id, raw_alias)
        self.sorted_aliases = []
        self._build_index()

    # ------------------------------------------------------------------
    def _build_index(self):
        for a in self.cfg["asset_dict"]["assets"]:
            self.assets[a["id"]] = a
            for alias in a["aliases"]:
                na = norm_text(alias)
                if not na:
                    continue
                prev = self.alias_map.get(na)
                # 同一别名指向多个资产时，保留先声明者（配置顺序即优先级），仅提示
                if prev and prev[0] != a["id"]:
                    print("  ⚠️ 别名冲突：%s → %s / %s（保留前者）" % (alias, prev[0], a["id"]))
                    continue
                self.alias_map[na] = (a["id"], alias)
        self.sorted_aliases = sorted(self.alias_map.keys(), key=len, reverse=True)

    # ------------------------------------------------------------------
    def resolve(self, name):
        """把一个名称（LLM 输出 / 候选池片段）回查成 asset_id；查不到返回 None。"""
        if not name:
            return None
        na = norm_text(name)
        if na in self.alias_map:
            return self.alias_map[na][0]
        # 包含关系兜底：只有当 name 里含唯一一个别名时才认
        hits = {aid for a, (aid, _raw) in self.alias_map.items() if len(a) >= 2 and a in na}
        if len(hits) == 1:
            return hits.pop()
        return None

    def is_investable(self, asset_id):
        return asset_id not in NON_INVESTABLE

    def name_of(self, asset_id):
        a = self.assets.get(asset_id)
        if not a:
            return asset_id
        return a.get("name") or asset_id

    # ------------------------------------------------------------------
    def match(self, text):
        """
        返回:
          matched:     [{"id","alias","conf"}]   按出现位置排序
          skipped:     [{"id","reason"}]         消解规则判为非资产
          candidates:  [片段]                    未命中的可疑片段（进候选池）
        """
        t = norm_text(text)
        if not t:
            return {"matched": [], "skipped": [], "candidates": []}

        mask = bytearray(len(t))
        hits = []
        for na in self.sorted_aliases:
            L = len(na)
            start = 0
            while True:
                idx = t.find(na, start)
                if idx < 0:
                    break
                if not any(mask[idx:idx + L]):
                    for i in range(idx, idx + L):
                        mask[i] = 1
                    hits.append((idx, na))
                start = idx + 1
        hits.sort()

        matched, skipped, seen = [], [], set()
        for _idx, na in hits:
            asset_id, alias_raw = self.alias_map[na]
            rule = RULES.get(asset_id)
            conf = 0.9
            if rule:
                target, conf = self._apply_rule(rule, t, asset_id)
                if target == "SKIP":
                    if asset_id not in {"skip:" + s["id"] for s in skipped}:
                        skipped.append({"id": asset_id, "reason": "消解规则：非资产语境"})
                    continue
                asset_id = target
            if asset_id in seen:
                continue
            seen.add(asset_id)
            matched.append({"id": asset_id, "alias": alias_raw, "conf": round(conf, 2)})

        return {"matched": matched, "skipped": skipped,
                "candidates": self._candidates(t, seen)}

    # ------------------------------------------------------------------
    @staticmethod
    def _apply_rule(rule, t, asset_id):
        for br in rule.get("branches") or []:
            if any(k.lower() in t for k in br.get("any") or []):
                return br.get("to", asset_id), br.get("conf", 0.9)
        target = rule.get("default") or asset_id
        return target, rule.get("default_conf", 0.9)

    # ------------------------------------------------------------------
    def _candidates(self, t, matched_ids):
        """2–4 字 n-gram 候选池；只保留出现 ≥2 次、非词典、非停用词的片段。

        ⚠️ 必须在「去标点」的文本上做 n-gram —— 否则 "了，" / "金，" 这类碎片会污染候选池。
        """
        seg = clean_text(t)[:300]
        cands = Counter()
        for n in (2, 3, 4):
            for i in range(len(seg) - n + 1):
                frag = seg[i:i + n]
                if frag in STOP_FRAGMENTS or frag in self.alias_map:
                    continue
                if not re.search(r"[\u4e00-\u9fff]", frag):
                    continue
                cands[frag] += 1
        out = []
        for frag, c in cands.most_common(30):
            if c < 2:
                break
            # 片段若只是某个已命中资产的别名子串，跳过
            if any(frag in na for na in self.alias_map if len(na) > len(frag)):
                continue
            out.append(frag)
            if len(out) >= 12:
                break
        return out


# ---------------------------------------------------------------------------
# 内置自测
# ---------------------------------------------------------------------------
SAMPLES = [
    ("新手小白求助，纳指QDII现在可以上车吗，016452这个基金怎么样", ["NASDAQ_QDII"], 0),
    ("金价又涨了，要不要定投黄金ETF 518880", ["GOLD_ETF_518880"], 0),
    ("备婚买三金，金镯子比金条划算吗", ["GOLD_JEWELRY"], 0),
    ("大A这波半导体行情，512480拿住不动", ["CN_BROAD_A", "SEMI_512480"], 0),
    ("算力看CPO光模块，通信ETF 515880冲了", ["CPO_515880"], 0),
    ("求券商开户推荐，佣金哪家低", [], 1),
    ("姐妹们攒金豆一个月一颗，积存金真香", ["GOLD_ETF_518880"], 0),
    ("存款利率又降了，钱搬去债基还是大额存单", ["DEPOSIT", "BOND_FUND"], 0),
    ("大饼突破前高了，币圈最近疯", ["BTC"], 0),
    ("英伟达财报后，美股和纳指还能追吗", ["NVDA", "US_BROAD", "NASDAQ_QDII"], 0),
    ("算力租赁需求爆了，AIDC 怎么看", ["AI_ETF"], 0),
    ("买房投资收租划算吗，租售比怎么算", ["HOUSE"], 0),
    ("首付凑齐了，但房价还在跌，买房还是租房", [], 1),   # 生活语境，按配置不计入投资热度
]


def run_smoke(verbose=True):
    d = AssetDict()
    print("=" * 62)
    print("asset_dict.py 内置自测（词典模式，无 LLM）")
    print("词典：%d 个资产 / %d 个别名" % (len(d.assets), len(d.alias_map)))
    print("=" * 62)
    failures = 0
    for text, expect_ids, expect_skip in SAMPLES:
        r = d.match(text)
        got = [m["id"] for m in r["matched"]]
        ok = set(expect_ids) <= set(got) and len(r["skipped"]) >= expect_skip
        if not ok:
            failures += 1
        if verbose or not ok:
            print("[%s] %s…" % ("PASS" if ok else "FAIL", truncate(text, 34)))
            print("       命中=%s  跳过=%s  候选=%s"
                  % ([(m["id"], m["conf"]) for m in r["matched"]],
                     [s["id"] for s in r["skipped"]],
                     r["candidates"][:5]))
    print("-" * 62)
    print("结果：%d/%d 通过" % (len(SAMPLES) - failures, len(SAMPLES)))
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--file":
        d = AssetDict()
        path = sys.argv[2]
        n = 0
        with io.open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                text = (obj.get("title") or "") + (obj.get("desc") or "")
                r = d.match(text)
                r["_title"] = truncate(obj.get("title") or obj.get("desc") or "", 40)
                print(json.dumps(r, ensure_ascii=False))
                n += 1
        print("# 共处理 %d 条" % n, file=sys.stderr)
    else:
        sys.exit(run_smoke())
