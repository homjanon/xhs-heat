# xhs-heat · 小红书投资热度雷达

每日聚合小红书上被讨论最多的**投资资产**：总榜 / 蹿升 / 新面孔 / 讨论阶段画像，并用雪球热帖做对照车道，回答「这是小红书独热还是全网共振」。

**描述型监测工具：不做预测、不做反向操作、不评判人群、不做回测。**
它只回答一个问题：今天小红书上的人在讨论什么资产、热到什么程度、处在哪个讨论阶段。怎么解读，归你。

线上看板：<https://xhs-heat.hellohopo.dpdns.org>

---

## 一、架构：本地采集 + 云端处理发布

```
本地 Windows（住宅 IP，扫码登录）
  collect_xhs.py ── 调 MediaCrawler ── 归一化 ── inbox/YYYY-MM-DD.jsonl.gz
                                                        │ push（GitHub API）
                                                        ▼
GitHub Actions（publish.yml）
  smoke_test → fetch_heat：去重/营销剥离 → 词典 → LLM 长尾 → 热度分 → 双留存
  对照车道：雪球热帖（RSSHub）+ 东财人气榜
                                                        │
                                                        ▼
  docs/latest.json + daily/ + agg/ + events.json + reports/ → GitHub Pages 看板
```

**为什么采集不在云端跑**：小红书按 IP 风控，Actions 的 Azure 段基本必挂；登录态需要扫码，CI 无法完成。
云端只负责「处理 + 发布」，所以哪怕采集断了，看板也不会空窗——会输出带水印的 stale 版。

## 二、每天怎么用

```bash
# 1) 采集（16:40 左右跑；首次会弹二维码，用小红书 App 扫码）
python scripts/collect_xhs.py --push

# 2) 云端自动处理并发布（push 触发），约 1 分钟出榜
#    看板：https://xhs-heat.hellohopo.dpdns.org
#    推送文本：docs/push/YYYY-MM-DD.txt
```

常用开关：

```bash
python scripts/collect_xhs.py --keywords 基金,黄金 --max-notes 20   # 小样本验证
python scripts/collect_xhs.py --dry-run                             # 只打印命令
python scripts/fetch_heat.py --demo                                  # 离线跑通全链路
python scripts/fetch_heat.py --date 2026-11-26 --no-llm               # 强制词典口径
python scripts/smoke_test.py                                         # 改配置后先跑这个
python scripts/probe_sources.py                                      # 探测各源健康
```

## 三、首次部署（一次性）

1. 本地装依赖并克隆 MediaCrawler：

```bash
pip install uv
git clone https://github.com/NanmiCoder/MediaCrawler.git ../_mc
cd ../_mc && uv sync && cd -
```

2. 采集一次（扫码）：

```bash
python scripts/collect_xhs.py --keywords 基金 --max-notes 20
```

3. GitHub 仓库 Secrets（Settings → Secrets → Actions）：

| Secret | 用途 | 后果 |
|---|---|---|
| `AGNES_API_KEY` | 主模型（免费不限量） | 缺失则长尾抽取走下一档 |
| `GEMINI_API_KEY` | 中间备份 | 缺失跳过 |
| `SENSENOVA_API_KEY` | 末位兜底 | 缺失跳过 |

三档全缺也能跑：自动切「词典模式」，榜单照出，只是长尾资产识别变弱（页头会标 `抽取：词典模式`）。

4. Pages：Settings → Pages → Source 选 `Deploy from a branch`，分支 `main`，目录 `/docs`。
   CNAME 已写好 `xhs-heat.hellohopo.dpdns.org`。

5. 定时：本地 Windows 任务计划 16:40 执行
   `python scripts/collect_xhs.py --push`（工作目录 `C:\Users\ho\WorkBuddy\xhs-heat`）。
   刻意不用 GitHub `schedule` —— 实测延迟可达 2h48m，且与手动触发并存会双跑。

## 四、配置驱动（只改 `scripts/sources.json`）

| 段 | 管什么 |
|---|---|
| `lanes.xhs.mode_a.entry_keywords` | 入口词池（35 个，覆盖「投资的入口」而非具体标的） |
| `asset_dict.assets` | 资产词典：36 资产 / 179 别名 + 消解规则 |
| `heat` | 热度权重、互动权重、归一窗口、冷启动阈值 |
| `hard_rules` | 营销识别词表、去重阈值、意图兜底词表 |
| `quality_gates` | 有效样本 ≥200、营销占比告警 40% 等 |
| `events` | 事件库入库门槛与容量 |
| `llm` | 三档模型链与批次上限 |

加资产、改词表、调阈值**都不需要动代码**；改完跑 `python scripts/smoke_test.py`。

## 五、口径说明

```
Heat = 100 × ( 0.45·V + 0.35·E + 0.20·M )
V 讨论量：自然笔记数（营销剔除，多资产按 1/n 分摊）
E 互动量：赞×1 + 藏×3 + 评×2 的对数和，单条贡献上限 10%
M 动量：讨论量 3 日增速，截断 [-2,+3]
```

- 历史满 8 天 → 90 日滚动分位归一（`caliber=rolling`）
- 历史不足 → 冷启动截面归一、动量取中性 0.5（`caliber=bootstrap`），页头明确标注，两种口径数值不可直接比
- 意图五分类：`学/买/晒/愁/广`；营销只剥离、不删除，占比照常展示
- 对照车道门槛：总提及 <6 或单资产 <2 → 标「参照样本不足」，不判双热/独热

## 六、目录

```
scripts/
  sources.json      唯一配置源
  asset_dict.py     词典匹配 + 消歧（纯规则，内置 13 条自测）
  hard_rules.py     去重 / 营销 / 意图兜底
  llm_chain.py      三档降级 + 熔断 + JSON 解析
  heat.py           聚合 / 热度 / 事件库
  reference_lane.py 对照车道（雪球热帖 + 东财人气榜）
  fetch_heat.py     主流程（读 inbox → 产物）
  collect_xhs.py    本地采集器
  smoke_test.py     离线冒烟（改配置后必跑）
  probe_sources.py  只读健康探测
docs/               看板与产物（Pages 根目录）
inbox/              本地采集件（按日，30 天后可清）
.github/workflows/  publish.yml / probe.yml
```

## 七、合规红线

- 只采公开笔记的标题/正文/互动量/时间；作者仅存哈希，不存昵称、头像、主页链接
- 不执行任何互动操作（点赞/关注/私信）；单词 ≤3 页、全局 QPS ≤0.5、串行不并发
- Cookie 与 API Key 只走本地环境变量 / GitHub Secrets，**绝不入仓**
- 看板与推送固定声明：聚合公开内容的舆情观察，不构成投资建议，不代表平台官方数据
- 触发风控（返回空 / 403 / 验证码）立即停 24h，不硬撞

## 八、实施记录（2026-11-26）

对 v3 设计的两处偏离，均已实测决定：

| 项 | v3 原方案 | 实际 | 原因 |
|---|---|---|---|
| 采集位置 | 云端 Actions 跑 MediaCrawler | **本地采集 → 推 inbox → 云端处理** | XHS 按 IP 风控；登录态需扫码 |
| 对照车道 | RSSHub 股吧路由 | 雪球热帖 + 东财人气榜 | 实测 `/guba/*` 在 4 个公共实例全 404；`/xueqiu/hots` 200/10 条；雪球直连 API 命中阿里云 WAF |
