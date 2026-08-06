# 003.Dramawave D7/D30 归因对比看板需求与技术设计

## 背景

Dramawave 原使用 D7 归因窗口，自 2026-07-29 起切换为 D30 归因窗口。线上真实表为：

- 旧口径：`kunlunads_dev.ads_app_revenues`
- 新口径：`kunlunads_dev.ads_app_revenues_30d`
- 当前投放统计与维度：`kunlunads_dev.ads_custom_source_insight`

只读抽样已证明 2026-07-29 的 `ads_custom_source_insight.af_revenue0/af_revenue` 与 D30 表一致，而与旧 D7 表存在差异。业务需要在同一筛选条件和同一花费分母下直观看到两种归因窗口的表现差异。

这里的“D7 / D30”是归因窗口。两张 revenue 表内部都同时包含 D0 收入和 D7 累计收入，页面不得把“D7 归因窗口”和“D7 累计收入”混为一个概念。

## 目标

1. 提供从 2026-07-29（北京时间，含当天）开始的 Dramawave 对比看板。
2. 同时展示旧 D7 归因窗口和新 D30 归因窗口下的 D0、D7 累计收入、ROAS 和差异。
3. 支持按时间、Campaign、Ad Set、优化师、国家组、渠道、投放产品、账户等维度筛选、组合分组和导出。
4. 页面查询只读本地 SQLite 缓存，不在用户请求内回源 MySQL。
5. 每 30 分钟刷新今天和昨天，保留近 60 天；另轮转回补一个历史日期，避免 D30 延迟归因使两天前数据永久变旧。
6. 常用查询保持后端式加载速度，不下载 60 天广告级明细到浏览器。

## 范围

### 包含

- 源数据范围固定为 `ads_custom_source_insight.product='Dramawave'`，`app_id` 作为“投放产品”子维度。
- 渠道使用 `ads_custom_source_insight.platform`；已知值 `0=Meta`、`1=Google`、`3=TikTok`、`4=Kwai`，未知值按 `渠道 <id>` 显示。
- 优化师使用 `optimizer` ID 分组，并关联 `admin_users` 的名称；名称缺失时保留 ID。
- revenue 表中的 Android/iOS 等 `platform` 值仅作为 OS 行合并，不作为投放渠道。
- 同时支持 D0 和 D7 累计指标基准切换。
- 服务端聚合查询、趋势、排行、分页/限制和 CSV 导出。
- Feishu 登录鉴权、私有缓存、gzip、健康检查、定时刷新和可回滚部署。

### 不包含

- 修改三张业务源表、创建远程 MySQL 表或写入 63353。
- 改写 `ads_custom_source_insight` 的生产口径。
- 按估算比例把 Campaign 级收入强行分摊到国家组或 Ad Set。
- 广告投放、暂停、预算或任何媒体渠道写操作。
- 复用 TT 小程序看板的全量静态明细下载架构。

## 用户故事 / 业务规则

1. 用户选择相同日期与维度后，可以并排看到旧 D7 归因和新 D30 归因的收入、ROAS、差额和提升率。
2. 花费只取 `ads_custom_source_insight.spend` 一次；两种 ROAS 必须使用相同筛选下的同一个花费分母，并在汇总后重新相除。
3. 默认指标基准为 D0：
   - `旧口径 D0 收入 = ads_app_revenues.revenue_iaa_d0 + revenue_iap_d0`
   - `新口径 D0 收入 = ads_app_revenues_30d.revenue_iaa_d0 + revenue_iap_d0`
4. 切换到 D7 累计指标时，分别使用两表的 `revenue_iaa_d7 + revenue_iap_d7`。
5. `收入差额 = D30 - D7`；`提升率 = (D30-D7)/D7`。D7=0 且 D30>0 显示“新增”，两者均为 0 显示“—”。
6. ID 全程按字符串保存和返回，避免 JavaScript 大整数精度丢失。
7. 今天的数据标记为未完结；开始日期、结束日期均包含。
8. revenue 记录必须先合并同一实体的 OS / data_source 行，再做映射，禁止原始明细直接 Join。
9. 映射优先级为 `日期+Ad`、`日期+Ad Set`、`日期+Campaign`。每条 revenue 聚合键只计一次。
10. 多候选映射不得用随机行、`MAX()` 或花费占比强行归属；应标记为 `ambiguous`，保留可确定的上层维度，无法确定的下层维度显示“映射歧义”。

## 交互与流程

- 日期快捷项：今天、昨天、近 3 天、近 7 天、近 30 天、全部可用日期。
- 筛选：渠道、投放产品 `app_id`、优化师、国家组、账户、Campaign 关键词、Ad Set 关键词。
- 分组维度：日期、Campaign、Ad Set、优化师、国家组、渠道、投放产品、账户、映射层级。
- 指标基准切换：D0 / D7 累计。
- 首屏优先返回 KPI、日趋势和当前聚合表；排行随后异步加载且同一筛选口径只计算一次，不阻塞主数据。表格大结果受服务端限制，完整结果通过后端 CSV 导出。
- 页面显示缓存版本、生成时间、源数据最大更新时间、缓存日期范围、映射覆盖情况和歧义提示。

## 技术设计

### 影响模块

- 新增 `ops/dramawave-attribution-comparison/` 独立服务。
- 新增只读聚合 API、SQLite ETL、静态前端、systemd service/timer 和 Nginx location。
- 不修改主 AI 后台业务状态机或远程 MySQL schema。

### 数据结构

本地事实表按可查询的最小必要粒度保存：

```text
dt + channel + product + app_id + optimizer_id + country_group
   + ad_account_id + campaign_id + adset_id
   + matched_grain + mapping_status
```

指标为可加总原子值：花费、曝光、点击、AF 安装，以及旧/新窗口各自的 users、purchase D0/D7、IAA/IAP D0/D7、广告展示。收入、ROAS、差额、提升率均在 API 聚合后计算。

SQLite 使用 WAL、`busy_timeout`，保留可查询最细事实，并同步维护去除 Campaign/Ad Set 的日级筛选汇总和 Campaign 日级汇总。API 按查询维度选择最小可用汇总层，只有 Ad Set 明细/搜索才回退最细事实，避免首屏重复扫描大事实表。全部目标日期先 staging，随后在同一个事务中整体覆盖事实、重建汇总并推进 `data_version`；任一日期失败均保留完整上一版。缓存和日志默认放在 `/mnt/data-disk/dramawave-attribution-comparison/`。

### API / 接口

- `GET /healthz`：服务、缓存、版本和陈旧状态。
- `GET /api/meta`：维度、指标、日期边界、刷新元数据和源表说明。
- `GET /api/options`：当前日期范围的低基数筛选项。
- `GET /api/query`：白名单筛选、分组、排序和限制；返回 totals、trend、rankings、rows。
- `GET /api/export.csv`：使用同一筛选和分组规则导出完整聚合结果。

普通 API 进程只以只读模式打开 SQLite，不包含 MySQL 凭据或回源逻辑。

### 刷新调度

- systemd timer 每 30 分钟运行一次 oneshot。
- 每轮必须刷新北京时间今天和昨天。
- 每轮再按 cursor 轮转刷新一个更早的缓存日期，使延迟 D30 归因在约 29 小时内被再次吸收。
- 删除早于 `max(2026-07-29, 今天-59天)` 的事实和刷新日志。
- 首次上线显式 bootstrap 2026-07-29 至今天；不得在页面请求中隐式补数。

### 异常与边界

- MySQL 端口不是 63350、`@@read_only != 1`、数据盘未挂载或不可写时失败关闭。
- 单日期源查询/校验失败时事务回滚，不清空旧缓存。
- `app_revenues*.platform` 不得进入渠道字段。
- revenue 映射后的金额不得高于映射前候选金额；歧义、候选内未映射，以及无法判定 Dramawave 产品归属而排除的日期级全源行数/金额必须分开体现在质量元数据中。全源排除量不得伪装成业务筛选后的数量。
- 查询日期限制在缓存可用范围内，维度、排序和筛选字段均走白名单。

## 验收标准

1. `2026-07-29` 可查，`2026-07-28` 被拒绝或自动收敛到起始日。
2. 点样本 `2026-07-29 / ad_id=120250136876120737`：D30 D0=225.54、D30 D7=245.23，并能与旧 D7 表值形成差异。
3. 同一筛选下缓存金额与两源表预聚合结果误差不超过 0.01，计数完全一致。
4. Android/iOS 多行不会放大收入，任一映射键最多计入一次。
5. ROAS、差额和提升率由汇总分子分母重新计算，不平均行级比率。
6. 页面请求和普通 API 查询产生 0 次源库访问。
7. 每 30 分钟刷新近 2 天，刷新后 45 分钟内显示新版本；失败不推进版本。
8. 缓存仅保留近 60 天，并始终不早于 2026-07-29。
9. 常用 30/60 天聚合 API 冷查询目标 p95 <= 1 秒，热查询目标 p95 <= 300 ms；首屏目标 <= 2 秒。
10. 前端不下载全量广告明细；CSV 由后端生成。
11. Feishu 未登录访问跳转登录，已授权用户可访问，页面与 API 均不公开缓存。
12. 本地单测、Python 编译、JavaScript 语法、Nginx 配置、服务健康和真实浏览器检查全部通过。

## 风险与已决策项

- 已按上下文把“29号”锁定为 `2026-07-29 00:00:00 Asia/Shanghai`。
- 产品范围按投放产品固定为 `product='Dramawave'`，`app_id` 作为可筛选子维度。
- 默认展示 D0，对比页同时提供 D7 累计切换，以避免遗漏两表已有的成熟收入字段。
- Google/UAC 缺失 Ad ID 时使用 Ad Set，再到 Campaign；歧义不做比例分摊。
- 仅刷新近 2 天不足以吸收 D30 延迟回流，因此在不改变 30 分钟近 2 天主要求的前提下增加单日轮转回补。

## 变更记录

- 2026-08-06：创建需求；完成只读表结构、索引、日期覆盖和 7 月 29 日点样本核验。
- 2026-08-06：SA 决定采用服务端 SQLite 聚合 API，放弃 TT 看板的全量浏览器透视架构。
