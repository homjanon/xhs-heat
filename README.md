# 资产热度雷达 · xhs-heat

两条腿分开走的资产热度观察：

- **资金侧**：散户真金白银的关注——东财人气榜、同花顺热榜、ETF 成交额与量比
- **出圈层**：全网级信号——抖音热榜财经条目（单独成层，不进热度加权）
- **社媒侧**：~~小红书~~ **已放弃**（2026-11-27，账号收到官方违规预警，详见下文）

两套口径各有单位，**不做直接换算，也不加总成一个分数**。

线上看板：<https://xhs-heat.hellohopo.dpdns.org>

---

## 一、页面内容（每天 16:10 自动更新）

| 区块 | 指标 | 数据源 |
|---|---|---|
| 概念热度 | 两榜个股的概念标签聚合 + **概念均值涨跌幅** + 映射到资产 | 东财/同花顺 |
| ETF 资金流 | 成交额、涨跌幅、**量比**（与自身近 5 日均值比，3 天后可用） | 腾讯行情 |
| 抖音出圈 | 财经/资产条目，红徽=资产、蓝徽=市场级 | iesdouyin |
| 散户人气榜 | 东财 × 同花顺双列名次、双榜共振、**新进榜/掉榜/名次变化** | 东财/同花顺 |
| 雪球热帖 | 资产级提及（旁证，样本薄），可跳原帖、展开全文 | RSSHub |
| 抖音出圈事件库 | 跨日累积：词条 / 名次 / 热值 / 连续在榜天数 | iesdouyin |

## 二、数据来源实测记录

| 结论 | 证据 |
|---|---|
| 东财人气榜只有 Top100 | `pageNo=2` 实测返回 0 只，不支持翻页 |
| 同花顺 `hot_rank_chg` 恒为 0 | 排名变化只能靠自己每日快照跨日算 |
| 东财 `sc` 带 `SZ/SH` 前缀 | 需去前缀转 6 位；北交所 4/8 开头走 `bj` 前缀 |
| 雪球 screener 不可用 | `order_by=tweet/follow7d` 全返回空列表，连续请求触发 `403 Seek IP Blacklisted` |
| 抖音热榜可用且无需登录 | `iesdouyin` 200/50 条；字段 `word/hot_value/label`，名次取数组下标 |
| 腾讯行情成交额在 f[37]（万元） | 与 f[35] 第三段（元）交叉验证一致 |
| 小红书不可行 | 新号当天收到违规预警；461 账号级软封，换 IP/等冷却均无效。代码归档 `scripts/_archive/` |

## 三、度量层 v3

```
Heat = 100 × (0.45·广度 + 0.35·深度 + 0.20·动量)
广度 = 去重作者数（原来用笔记数，与互动总和共线，45+35 实际是同一信号 80%）
深度 = 单篇互动中位数（原来用互动总和）
动量 = 日历 3 天前对比，截断 [-2,+3]；缺数据就是缺（basis=missing_3d），不靠近邻替代
```

- 历史满 30 天 → 90 日滚动分位数归一（`caliber=rolling`）；不足 → 冷启动截面归一
- **口径切换日标 `trend="rebased"`，不算 delta_heat**（否则全榜跳变被误读成行情）
- 单条贡献上限 10%，迭代收敛（原一步公式实际占比 >10%）

回归测试：`python scripts/test_metrics.py`（11 项，合成历史验证日历对齐/正交化/rebased/断档）

## 四、每天怎么跑

**已定时**：Cloudflare Worker `qdii-dispatch` 的 **16:10** 槽位（含周末）触发 `publish.yml`，
自动走「只跑参考车道」模式（`mode=reference-only, collect=skip`）。

手动触发：

```bash
gh workflow run publish.yml -f mode=reference-only -f collect=skip
```

本地调试：

```bash
python scripts/fetch_heat.py --reference-only   # 参考车道
python scripts/smoke_test.py                    # 配置守门（39 项）
python scripts/test_metrics.py                  # 度量回归（11 项）
python scripts/probe_sources.py                 # 数据源健康探测
```

## 五、定时触发（已接入）

- Worker：`qdii-dispatch`，槽位 **16:10**（错开 douban 16:00 / xiaoxu 16:20 / cmb 16:30）
- 触发参数：`{ mode: "reference-only", collect: "skip" }`
- 手动补跑：`curl "https://qdii-dispatch.homjanon.workers.dev/trigger?repo=xhs-heat&key=<DISPATCH_KEY>"` 期望 204

## 六、存储与清理

每次发布前自动跑 `scripts/retention.py`，只删白名单目录、超期或超量按最旧优先：

| 目录 | 保留 | 用途 |
|---|---|---|
| `docs/data/popularity/` | 90 天 | 人气榜每日快照（新进榜/掉榜/名次变化依赖它） |
| `docs/data/etf/` | 180 天 | ETF 成交额快照（量比依赖它） |
| `docs/data/douyin/` | 180 天 | 出圈事件历史（同时进 `events.json` 的 breakouts） |
| `docs/data/agg/` | 400 天 | 热度分位基线 |
| `docs/data/reports/` `docs/push/` | 90 天 | 运行报告 / 推送文本 |

## 七、配置驱动（只改 `scripts/sources.json`）

| 段 | 管什么 |
|---|---|
| `lanes.reference.sources` | 车道开关与来源（东财/同花顺/雪球/抖音/ETF） |
| `asset_dict.assets` | 资产词典：36 资产 / 179 别名 |
| `asset_dict.concept_map` | 概念→资产映射（29 条，映射不上只显示数量） |
| `heat` | 热度权重（breadth/depth/momentum）、归一窗口、cap 迭代次数 |
| `storage` | 各目录保留天数与上限 |

改完跑 `python scripts/smoke_test.py`。

## 八、目录

```
scripts/
  sources.json      唯一配置源
  reference_lane.py 资金侧车道：两榜合并/概念聚合/快照/日环比
  douyin_hot.py     抖音热榜 → 出圈事件（进事件库）
  etf_flow.py       ETF 成交额 / 量比
  heat.py           聚合 / 热度 / 事件库（v3 日历对齐）
  asset_dict.py     词典匹配 + 消歧（内置 13 条自测）
  fetch_heat.py     主流程（含 --reference-only）
  retention.py      存储上限与到期清理
  smoke_test.py     配置守门（39 项）
  test_metrics.py   度量回归（11 项）
  llm_chain.py      三档降级（XHS 恢复才用得上，保留）
  probe_sources.py  数据源健康探测
  _archive/         小红书采集代码（已放弃，留档）
docs/               看板与产物（Pages 根目录）
.github/workflows/  publish.yml / probe.yml
```

## 九、合规

- 只采公开榜单数据，无登录、无个人信息、无互动操作
- 全局限速、失败退避、461/403 立即停手
- Cookie 与 API Key 只走 GitHub Secrets / 本地环境变量，绝不入仓

## 十、实施记录（摘要）

- 2026-11-26：立项，v3 设计落地；A2 实验证明云端可采小红书
- 2026-11-27：小红书账号收到违规预警 → 永久放弃社媒侧；接入东财/同花顺/ETF/抖音四条车道；
  度量层 v3 重写（日历对齐 / 广度深度正交 / 30 天 rebased / cap 迭代）；站点改名「资产热度雷达」；Worker 16:10 接入
- 外部评审逐条核实后采纳；其中「雪球 screener 可用」实测不成立，已否决
