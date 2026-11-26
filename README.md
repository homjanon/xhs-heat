# xhs-heat · 小红书投资热度雷达

每日聚合小红书上被讨论最多的**投资资产**：总榜 / 蹿升 / 新面孔 / 讨论阶段画像，并用雪球热帖做对照车道，回答「这是小红书独热还是全网共振」。

**描述型监测工具：不做预测、不做反向操作、不评判人群、不做回测。**
它只回答一个问题：今天小红书上的人在讨论什么资产、热到什么程度、处在哪个讨论阶段。怎么解读，归你。

线上看板：<https://xhs-heat.hellohopo.dpdns.org>

---

## 一、架构：全云端，无需本地机器

```
Cloudflare Worker qdii-dispatch（心跳，16:40 北京）
        │ workflow_dispatch
        ▼
GitHub Actions（publish.yml）
  ① fetch_xhs.py    requests + xhshow 纯算法签名 + cookie → inbox/YYYY-MM-DD.jsonl.gz
  ② fetch_heat.py   去重/营销剥离 → 词典 → LLM 长尾 → 热度分 → 榜单/事件库/报告
  ③ 参考车道        雪球热帖（RSSHub）+ 东财人气榜
  ④ 提交 docs/ → GitHub Pages 看板
```

**为什么不需要浏览器/MediaCrawler**（2026-11-26 实测）：小红书签名已可由 `xhshow` 纯算法生成，
唯一必需的是**登录 cookie**。实测从 Azure 机房 IP 用同一 cookie 正常取到 44 条真实笔记（赞/藏/评齐全），
所以采集可以完全跑在 Actions 里。

## 二、风控纪律（实测出来的，不是猜的）

| 事实 | 数据 | 对策 |
|---|---|---|
| 匿名访问拿不到数据 | 机房 200 但被 302 到登录页，`noteId=0` | 必须登录 cookie |
| 匿名访客**也有** `web_session` | 654B 游客 cookie | 判登录必须调 `/user/me` 看 `guest=false` |
| **461 是账号级限流** | 换全新 Azure IP 仍 461；`user/me` 正常 | 换 IP 没用，只能控量 |
| 触发阈值 | 连续 12–14 次搜索请求后开始 461 | **每天 15 个词 × 1 页**，按日轮换词池 |
| 请求间隔 | 2s 间隔时更快触发 | **5–7s 间隔**，461 → 退避 90s，连续 2 次立即中止 |

保守起步，连续 3 天无 461 再考虑扩容；扩容优先「多账号」而不是「加频率」。

## 三、每天怎么跑

由 CF Worker 心跳触发，无需人工：

```bash
# 手动补跑（采+发）
gh workflow run publish.yml -f collect=yes

# 只处理已有 inbox（不采集）
gh workflow run publish.yml -f collect=no

# 强制词典口径（不调 LLM）
gh workflow run publish.yml -f mode=no-llm
```

本地调试：

```bash
python scripts/fetch_xhs.py --limit 3 --delay 8     # 小样本
python scripts/fetch_heat.py --demo                  # 离线跑通全链路
python scripts/smoke_test.py                         # 改配置后先跑这个
python scripts/probe_sources.py                      # 探测各源健康
```

## 四、首次部署

1. **Secrets**（Settings → Secrets → Actions）

| Secret | 必需 | 用途 |
|---|---|---|
| `XHS_COOKIE` | ✅ | 小红书登录 cookie；过期需重扫 |
| `AGNES_API_KEY` | 建议 | 主模型（免费不限量） |
| `SENSENOVA_API_KEY` | 可选 | 第一档（polo 指定顺序） |
| `GEMINI_API_KEY` | 可选 | 末位兜底 |

  LLM 链顺序（2026-11-26 定稿）：**Agnes 3.0-flash → Gemini 3.5-flash-lite → SenseNova deepseek-flash**。
  429 只冷却 60 秒后重试，不整轮拉黑；403/404/410 永久跳过；全失败切「词典模式」，榜单照出。

  ⚠️ 实测：商汤这个 key 的 RPM 极紧（`RateLimitExceeded.EndpointRPMExceeded`），
  故下沉到末位，只在前两档都不可用时兜底。

2.1 **存储上限**（`sources.json` → `storage`）

| 目录 | 保留 | 上限 |
|---|---|---|
| `inbox/` 采集件 | 30 天 | 200 MB（超出按最旧优先删） |
| `docs/data/reports/` | 90 天 | — |
| `docs/push/` | 90 天 | — |
| `docs/daily/` | 365 天 | — |
| `docs/data/agg/` | 400 天 | 热度分位基线，留最长 |

每次发布前自动跑 `scripts/retention.py`；只删白名单目录内的文件，越界直接跳过。

2. **Pages**：Settings → Pages → Source 选 `Deploy from a branch`，分支 `main`，目录 `/docs`。

3. **DNS（Cloudflare）**：

| 类型 | 名称 | 内容 | 代理状态 |
|---|---|---|---|
| CNAME | `xhs-heat` | `homjanon.github.io` | **DNS only（灰云）** |

   灰云是必须的：橙云代理会让 GitHub 无法签发 Pages 证书。
   记录生效后回 Pages 页面点 `Check again` → 勾 `Enforce HTTPS`。

4. **CF Worker**：`qdii-dispatch` 里加一个槽位，北京时间 16:40 触发：

```js
// 现有 worker 里加一个槽位即可（与 qdii / portfolio 同构）
const XHS_HEAT = {
  repo: "homjanon/xhs-heat",
  workflow: "publish.yml",
  at: { hour: 16, minute: 40 },          // Asia/Shanghai
  body: { ref: "main", inputs: { collect: "run" } },
};
// POST https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches
// Authorization: Bearer <GitHub PAT>（已存在的那把即可）
```

## 五、cookie 维护（唯一的人工步骤）

cookie 有效期通常数周。失效时 workflow 会打 `::warning title=cookie 失效::`，看板转 stale。

```bash
python scripts/get_xhs_cookie.py      # 用本机 Edge 弹二维码，扫码
gh secret set XHS_COOKIE < .xhs_cookie.txt
```

脚本会调 `/user/me` 确认 `guest=false` 才保存——避免把游客态当成登录态（这个坑踩过一次）。

## 六、配置驱动（只改 `scripts/sources.json`）

| 段 | 管什么 |
|---|---|
| `lanes.xhs.mode_a.entry_keywords` | 入口词池（35 个） |
| `lanes.xhs.mode_a.daily_keyword_limit` | 每日取几个词（默认 15，按日轮换） |
| `asset_dict.assets` | 资产词典：36 资产 / 179 别名 + 消解规则 |
| `heat` | 热度权重、互动权重、归一窗口、冷启动阈值 |
| `hard_rules` | 营销识别词表、去重阈值、意图兜底词表 |
| `quality_gates` | 有效样本 ≥200、营销占比告警 40% |
| `events` | 事件库入库门槛与容量 |
| `llm` | 三档模型链与批次上限 |

## 七、口径

```
Heat = 100 × ( 0.45·V + 0.35·E + 0.20·M )
V 讨论量：自然笔记数（营销剔除，多资产按 1/n 分摊）
E 互动量：赞×1 + 藏×3 + 评×2 的对数和，单条贡献上限 10%
M 动量：讨论量 3 日增速，截断 [-2,+3]
```

- 历史满 8 天 → 90 日滚动分位归一（`caliber=rolling`）
- 历史不足 → 冷启动截面归一、动量取中性 0.5（`caliber=bootstrap`），页头标注，两种口径数值不可直接比
- 意图五分类：`学/买/晒/愁/广`；营销只剥离、不删除，占比照常展示
- 对照车道门槛：总提及 <6 或单资产 <2 → 标「参照样本不足」，不判双热/独热
- ⚠️ 搜索接口只返回**标题**（无正文），匹配与分类都基于标题；这是当前口径的已知上限

## 八、目录

```
scripts/
  sources.json      唯一配置源
  fetch_xhs.py      云端采集器（cookie + xhshow 签名 + 限速/退避/轮换）
  get_xhs_cookie.py 本机扫码取 cookie（唯一人工步骤）
  asset_dict.py     词典匹配 + 消歧（内置 13 条自测）
  hard_rules.py     去重 / 营销 / 意图兜底
  llm_chain.py      三档降级 + 熔断 + JSON 解析
  heat.py           聚合 / 热度 / 事件库
  reference_lane.py 对照车道（雪球热帖 + 东财人气榜）
  fetch_heat.py     主流程（读 inbox → 产物）
  smoke_test.py     离线冒烟（改配置后必跑）
  probe_sources.py  只读健康探测
  probe_xhs_cloud.py 采集探针（实验用，可随时验证 cookie/IP）
  retention.py      存储上限与到期清理
  collect_xhs.py    MediaCrawler 本地方案（已弃用，仅备用）
docs/               看板与产物（Pages 根目录）
inbox/              每日采集件
.github/workflows/  publish.yml / probe.yml
```

## 九、合规红线

- 只采公开笔记的标题/互动量/发布时间；作者仅存哈希，不存昵称、头像、主页
- 不执行任何互动操作（点赞/关注/私信）；串行、限速、每日限量
- Cookie 与 API Key 只走 GitHub Secrets / 本地 `.env.local`，**绝不入仓**
- 看板与推送固定声明：聚合公开内容的舆情观察，不构成投资建议，不代表平台官方数据
- 触发 461 立即停手，不在冷却期硬撞

## 十、实施记录（2026-11-26）

| 决策 | 依据 |
|---|---|
| 采集从「本地 MediaCrawler」改为「云端轻量采集器」 | A2 实验：Azure IP + cookie 取到 44 条真实笔记；签名已可纯算法完成 |
| 放弃 MediaCrawler | 30MB 依赖 + jieba 编译失败 + 需浏览器；新方案两个 pip 包即可 |
| 每日 15 词、按日轮换 | 461 账号级限流，实测连续 12–14 次触发 |
| 对照车道用雪球热帖 + 东财人气榜 | 股吧 RSSHub 路由 4 实例全 404；雪球直连 API 命中阿里云 WAF |
