# CTB-000102.opay-excellent-creatives-report 需求与技术设计

## 背景

OPay 投放团队需要按月复盘 Google、Meta、TikTok 的优秀素材。原方案拟写入飞书多维表格，现已确认改为 `ai.yingliangads.com` 下的全新公开只读报表，并将“创建人”调整为素材制作者。

## 目标

- 提供 `https://ai.yingliangads.com/reports/opay-excellent-creatives/`，无需登录即可查看。
- 首次回填 `2026-01` 至最近完整月份；北京时间每月 3 日生成初版、5 日生成终版。
- 浏览器只读取版本化静态 JSON，不访问 MySQL。
- 所有入选素材可追溯到真实 `ads_custom_source.id`、平台指标、AF 严格 D0 首交和入选证据。

## 范围

### 包含

- 素材产品：`ads_custom_source.product` 大小写兼容 `OPay/Opay`。
- 数据产品：`OPay`、`OPay NGN` 归为 `NG OPay`；`OPayPakistan` 归为 `PK OPay`，比较时大小写不敏感。
- 渠道：Meta（0）、Google（1）、TikTok（3）。
- 粒度：月份 × 渠道 × App × `ads_custom_source.id`。
- 主表、详情面板、审计区、筛选、预览、源文件链接、CSV 导出。
- 图片/视频缩略图缓存、制作者解析、卖点关键词匹配、人工覆盖配置。

### 不包含

- 不写飞书多维表格，不上传飞书附件。
- 不统计 OperaNews、OPayBusiness、OPayEG、W2A OPay 或其他非本需求产品。
- 不把平台首交混入 AF D0 首交。
- 不对 Google 广告组消耗或 AF 首交做素材分摊，不生成估算值。
- 不修改 AI Game Performance 报表、其鉴权或刷新任务。

## 用户故事 / 业务规则

### 指标口径

- 平台指标来自 `kunlunads_dev.ads_custom_source_insight`：`spend`（已归一 USD）、`impressions`、`clicks`、`installs`。
- 汇总包含目标 App 下全部账户和国家。
- AF 严格 D0 首交来自 `ads_af_revenues_zone`，固定 `data_source=0`，使用 `revenue_event_count1_0`。
- 产品配置必须动态验证 OPay/OPayPakistan 的 `revenue_evente` 包含精确事件 `First_Transaction`；配置不满足则整月生成失败并保留上一版本。
- 平台整体 CTR = 点击 / 曝光；平台整体 CPA = 消耗 / AF D0 首交数。

### 优秀规则

- 规则 A：素材位于累计消耗前 50%，且素材 AF D0 首交 CPA 严格小于同月同渠道同 App 平台整体 CPA。
- 规则 B：素材月消耗严格大于 5000 USD，且素材 CTR 严格大于平台整体 CTR。
- 两条规则为 OR；同时满足显示 `A+B`。
- 按素材消耗降序累计，以平台整体消耗的 50% 为阈值；包含第一组跨过阈值的素材及该消耗值的全部并列素材。
- AF D0 首交为 0 时素材 CPA 为无穷大，A 不通过；曝光为 0 时 CTR 为 0。
- 精确映射素材消耗 / 平台消耗小于 50% 时，规则 A 整个渠道/App 暂停。
- 无优秀素材时不生成占位行，审计区显示“计算成功，入选0条”。

### 平台映射

- Meta/TikTok：`ads_custom_source_insight.source_type=3`，且 `source_id` 必须精确回连 `ads_source.id`、`resource_id` 必须等于 `ads_source.source_id` 和 `ads_custom_source.id`。同一广告日对应多个素材时整组排除并审计。
- Google：平台基准仍按平台事实计算；V1 只接受素材指标、素材关系和 AF 归因全部精确且唯一的记录。当前仓库缺少可证明完整的素材级 USD 消耗与 AF 粒度，默认审计为严格排除，不使用广告组或资产占比分摊。

### 素材与卖点

- 素材 ID 固定为永久 `ads_custom_source.id`。
- `type=1` 显示 PIC，`type=2` 显示 VID。
- 图片缓存缩略图；视频优先使用 `cover`，缺失时用 FFmpeg，服务器无 FFmpeg 时使用受超时控制的 OpenCV 子进程提取代表帧。
- 已知 COS HTTP 地址升级为 HTTPS；源文件不镜像，失效时保留报表行并显示异常。
- 制作者取 `designer`：数字按 `admin_users.id`，字符串按 `admin_users.username`；依次显示 `main_username`、`name`、`未登记`。
- 首次上线时间优先使用最早 `auto_publish_dt`，否则回退最早实际投放 `dt`，详情显示来源。
- NG 以“素材标签（上传盈量素材系统）”匹配，展示“关键词（广告平台命名）”，空值回退上传标签；PK 使用二级分类。
- 精确标签优先；无精确匹配时按边界、最长且不重叠的别名匹配标签/文件名；允许多卖点。
- 失效卖点仍可按绩效入选并标记；未匹配显示“待补关键词”；支持按素材 ID 的 JSON 人工覆盖。

## 交互与流程

1. 默认打开最近完整月份，按消耗降序。
2. 可按月份、渠道、App、素材类型、关键词、制作者、入选规则筛选。
3. 点击缩略图打开图片/视频弹窗；“源文件”在新窗口打开原始 HTTPS 地址。
4. 点击行查看素材与平台 CTR/CPA、排名、累计占比、时间来源和数据完整性。
5. CSV 导出严格使用当前筛选结果和当前排序。
6. 顶部展示版本、初版/终版、生成时间；审计区固定展示 3 渠道 × 2 App。

## 技术设计

### 影响模块

- 新增 `ops/opay-excellent-creatives/` 生成器、静态前端、关键词配置、Nginx 和测试。
- 新增 `deploy/opay-excellent-creatives-*` systemd/env 文件。
- 新增本需求全流程文档。
- 不改主 API 和已有报表代码。

### 数据结构

持久根目录 `/mnt/data-disk/opay-excellent-creatives`：

- `cache/opay-excellent-creatives.sqlite3`：按日平台事实、精确素材事实、AF 事实、素材维度和发布审计。
- `thumbnails/`：按素材 ID 缓存的同源缩略图。
- `snapshots/<month>/<stage>/`：不可变初版/终版快照与差异审计。
- `backups/`：部署回滚包。

SQLite 仅作本地缓存，不新增 MySQL 表、不执行 MySQL 写入或 DDL。

### API / 接口

- `GET /reports/opay-excellent-creatives/`：公开 HTML。
- `GET /reports/opay-excellent-creatives/latest.json`：当前提交清单。
- `GET /reports/opay-excellent-creatives/data/<version>/<yyyy-mm>.json`：版本化月数据。
- `GET /reports/opay-excellent-creatives/assets/thumbnails/<file>`：缓存缩略图。

CLI 支持单月初版/终版、最近完整月份、历史回填、缓存检查和显式重建冻结月份。

### 异常与边界

- 任何数据库读取、计算或发布失败均不得替换 `latest.json`。
- 关键词、缩略图或单个源文件检查失败只降级该行。
- 终版默认冻结；只有 `--rebuild` 可重算。
- `latest.json` 最后原子替换，是公开版本提交点。
- 媒体只接受明确允许的 HTTP(S) 主机，防止由数据库 URL 触发 SSRF。
- 页面带 `noindex,nofollow` 与 `X-Robots-Tag`；这不是访问控制，持有链接者可看到素材和制作者。

## 验收标准

- 页面和 JSON 匿名访问返回 200，无飞书跳转。
- `2026-01` 至最近完整月份均存在终版并可切换。
- Meta/TikTok 产出严格映射结果；Google 缺口清晰且无估算行。
- 严格 `<`/`>`、OR、50% 跨线与并列、零值、歧义和覆盖门槛测试通过。
- 抽样花费、CTR、AF D0 首交和平台基准与只读 SQL 一致。
- 所有素材 ID 等于真实 `ads_custom_source.id`；素材产品均为 OPay。
- 缩略图、源文件状态、制作者解析、关键词状态与详情证据可见。
- 定时器、原子发布、失败保留旧版本和回滚流程验证通过。

## 风险与已确认边界

- 已确认采用公开链接。`noindex` 不能阻止链接转发或未授权查看，这是接受的产品风险。
- Google 当前精确素材级数据契约不足，V1 允许 0 行但必须显示覆盖缺口；后续只有在仓库补齐精确 USD 素材指标和 AF 归因后才开放。
- 历史源文件可能失效，属单行降级，不影响绩效行保留。

## 变更记录

- 2026-08-26：用户确认仅统计 OPay；素材产品为 OPay，数据产品为 OPay NGN/OPay Pakistan。
- 2026-08-26：输出从飞书多维表格调整为 `ai.yingliangads.com` 新公开报表。
- 2026-08-26：创建人字段调整为素材制作者。
- 2026-08-26：用户批准本需求与实施方案，进入开发。

## V2 增量需求与技术设计（2026-08-27）

本节为 V2 契约；以上 V1 需求、0 行 Google 的历史边界及验收证据全部保留，不追改为 V2 已完成。V2 仅替代 Google 数据来源/可用性与派生指标展示约定；未明确变更的 OPay 范围、Meta/TikTok 选优和归因、关键词、媒体、公开只读、初终版冻结规则继续生效。本次文档补充不代表代码、回填、发布或独立 QA 已通过。

### V2-REQ-01：Google 素材事实与严格映射

- Google（简称 GG，公开渠道名仍为 `Google`）素材事实来自 `kunlunads_dev.ads_google_insights` 的 `type=3`，仅接收 `asset_type=2`（video）、`asset_type=4`（image），分别展示 `VID`、`PIC`；不得与自建素材 `ads_custom_source.type=2/1` 混为同一枚举。
- 必须完整走通 `mapping.asset_name → ads_source(source_type=3) → ads_custom_source.id`。当前实现契约为 `insight.resource_id = mapping.asset_name`，mapping 来源 `ads_google_resource_mapping`，`mapping.source_id = ads_source.id`，并要求 `ads_source.source_id = mapping.resource_id = ads_custom_source.id`。素材产品仍只接受 `OPay/Opay`，App 仍限定 NG OPay / PK OPay；这些连接列须由独立只读 schema/代码评审核验，不将本次文档对齐当生产验证。
- 在事实所属的账户/资产上下文中做精确相等连接，禁止文件名相似、前后缀截取、模糊搜索、广告组或消耗比例分摊。无法证明唯一回连的事实不得入选。
- 先验证全部候选source链，再去重并聚合事实；不能过滤非法候选后只凭剩余好链判exact。全部链合法且一致回连同一自建素材时，多广告复用/重复路径仍只计一次；任一链非法则排除，最终目标冲突则整条事实按歧义排除并审计，不取第一条、不累加join放大的指标。不同日期/账户的真实事实按唯一键保留，不按指标值去重。
- 无映射、非 OPay、枚举不支持、路径歧义分别记录原因、原始事实数及可核验消耗；未知金额不填 0。同月同渠道同 App 同素材只输出一行。

### V2-REQ-02：平台基准与历史汇率

- GG 平台基准单独读取 `ads_google_insights.type=0`，按同月同 App 的全部目标账户/国家汇总。不得用入选素材、仅已映射素材、`type=3` 资产总和或旧 V1 Google 事实替代，也不得把 `type=0` 与 `type=3` 相加。
- 每个GG asset-day须有同App、同账户、同日的Campaign `type=0` account-day。任一缺失时，该月该App的GG规则B整体暂停，基准曝光/点击/CTR为null，审计`baseline_missing_account_days`记录缺口；不能借其他账户/日期、不能把缺行当真实0。这与“只有FX缺失、type0基准完整时CTR仍可用”是两种不同状态。
- GG `cost` 是 micros：`原币金额 = Decimal(cost) / 1_000_000`；`USD 金额 = 原币金额 × 事实日期适用的 USD/原币历史汇率`。若源汇率方向相反，核验后显式取倒数一次。按事实日期逐笔换汇后汇总，不能先按月汇总原币再套当前汇率；Meta/TikTok 已归一 USD 的 spend 不再换汇。
- 当前历史FX候选来自`ads_platform_report_items`同日/同账户的`exchange_rate`、`last_exchange_rate`，方向原币/USD，换算为`原币 / rate`。以历史`spend`、`spend_usd`核验唯一候选（逐条正消耗记录按分取整后USD误差不超过0.01）；空/非法候选跳过，另一候选仍可独立核验。正消耗历史行缺`spend_usd`属于无法对账的缺口，不能跳过该行/补0；不能默认current列或用`spend/spend_usd`发明汇率/比例分摊。
- Decimal 换汇值先按素材月汇总，再沿用金额分单位 `ROUND_HALF_UP` 归一一次；规则 B 比较归一后的 `spend_cents > 500000`，不用展示字符串或逐日先舍入的和代替。
- 每笔需能追溯原币种、原始 micros、事实日期、汇率方向、历史有效日期/区间、来源及版本。USD 只有在币种明确为 USD 时才使用恒等汇率 1；未知币种不得默认 USD。
- 历史汇率须可核验且覆盖事实日。剔除空/非正数/非有限候选后，若仍无唯一可核验汇率、历史USD证据缺失、方向不明或有效证据冲突，则金额fail-closed；禁止当前汇率回填历史、猜固定汇率、未经证明的邻日回退，亦不得用平台/广告组金额反推素材金额。
- 真实读取的 `cost=0` 是已知零，即使没有 FX 行也可记 USD 0，并标记零消耗而非“汇率已核验”；不能把缺失 cost 或读取失败当作这个特例。
- 素材任一相关事实的 USD 无法核验，则该素材月消耗不完整，不得用已知部分判定规则 B；记录缺口，不生成估算入选行。其他具有完整 USD 的素材仍可独立判定。
- `type=0` 存在无法核验的金额时，GG 平台 `spend`、`cpa`、映射 `coverage`/gap 等依赖金额的结果可为 `null`，禁止把已知部分冒充完整平台消耗。完整的 `type=0` 曝光和点击仍须保留并计算 CTR；平台金额缺失不应连带禁用可用的 CTR 或符合条件的规则 B。
- 映射覆盖分子为可核验的精确映射USD子集，同时披露未知FX行/原币缺口：`fx_missing_native_spend`记录素材type3，`platform_fx_missing_native_spend`记录Campaign type0，按币种分组，不跨币种相加或标USD。平台分母不完整则coverage=null，明确已知零消耗沿用V1的coverage=0。覆盖率只作质量说明，不解锁A；不把可核验子集冒充完整金额或靠分摊掩盖重复映射。
- 汇率/映射业务缺口属于明确审计的降级结果；数据库读取不完整、计算异常或发布失败仍为硬失败，保留上一公开版本。不能把查询失败当成“无数据”或全 0。

### V2-REQ-03：GG 仅规则 B，缺失不等于零

- GG 唯一入选规则为 B：完整可核验的素材月消耗严格 `> 5000 USD`，且素材 CTR 严格大于同月同 App 的 GG `type=0` 平台 CTR。消耗等于 5000、CTR 相等均不通过；比较使用未展示舍入的精确值/交叉乘积。
- 素材曝光为 0 时 B 不通过；平台曝光明确为 0 时 CTR 按 V1 的已知零口径比较，曝光/点击缺失则不可补零推定优势。金额未知不能按 0 比较；平台金额未知但 CTR 完整时，仍可对金额完整的素材判断 B。
- GG `rule_a_available=false`、`rule_a_pass=false`，入选标记只能为 `B`，即使消耗排名/覆盖满足 V1 门槛也不能出现 `A` 或 `A+B`。
- GG 素材行 `rows[]` 的 `installs`、`af_d0_first_transactions` 均为 `null`；依赖它们的素材指标不可计算。平台若保留已有可核验的聚合 AF D0，只能作平台基准证据，不得回填素材；平台安装无可信来源仍为 null，平台 CPA 在 USD/AF 不可用时为 null。禁止把 Google `conversions` 当安装、AF D0 首交或规则 A 的依据。
- Google `conversions`允许有限非负小数（不是整数计数），仅在详情说明来源并保留源值精度；负数、NaN/Infinity不合法，不进入主表、主CSV、六项指标的分子/分母或入选规则。
- `null` 表示不可用/无法计算；真实观测的 `0` 仍是数值 0。前端和 CSV 不得用 `value || 0`、`Number(null)` 等把缺失转零，GG 缺失 CPA 不展示成“0 USD”或“∞ 首交成本”。

### V2-REQ-04：六项指标与 schema 2

- 月数据与公开清单升级为 `schema_version=2`，保留 `data_version`、原始行字段及 Meta/TikTok 既有证据；六项派生值统一放在 `row.metrics`，不是 row 根字段，也不是新造的 `latest.json.version`。
- 六项必须先汇总同月同素材的原始分子/分母再计算，不能平均日 CTR/成本或用前端格式化后的数值计算；展示舍入不得参与选优。
- 六项键和公式按当前并行实现对齐如下；`S`=USD 消耗，`I`=曝光，`C`=点击，`N`=安装，`A`=AF 严格 D0 首交。任一所需输入缺失，结果为 null；不能用 conversions 替代 N 或 A。

| 指标 | row.metrics 键 | 正常公式/单位 | 真实零及零分母 | GG 素材 |
| --- | --- | --- | --- | --- |
| D0 首交 CPA | `d0_cpa` | `S / A`，USD/首交 | A>0且S=0得0；A=0时JSON为null，逻辑成本∞，仅已知零首交可显示∞ | null |
| CPM | `cpm` | `S / I × 1000`，USD/千次曝光 | I>0且S=0得0；I=0时null | 可计算 |
| APM | `apm` | `A / I × 1000`，AF D0首交/千次曝光 | I>0且A=0得0；I=0时null | null |
| CTR | `ctr` | `C / I`，比率 | C、I均已知时I=0沿用V1得0；C=0且I>0得0；输入缺失仍null | 可计算 |
| CVR | `cvr` | `N / C`，点击→安装比率 | C>0且N=0得0；C=0时null | null |
| 安装→D0首交转化率 | `install_to_d0_rate` | `A / N`，比率 | N>0且A=0得0；N=0时null | null |

- CPA/CPM JSON输出最多6位小数，其余最多8位，所有row及benchmark均有同形metrics。CTR/CVR/安装→D0首交在页面乘100显示百分数；APM是“次/千曝光”不是百分数，页面表格/详情固定4位，CSV保留JSON原始精度（最多8位），不能沿用页面舍入。不把真实大于1的比率截成100%；展示精度不改变入选比较。
- `metrics` 数值只能为有限 JSON number 或 `null`，禁止 `NaN`、`Infinity`、字符串 `"null"`。依赖的原始数据缺失时不可推导；分子确为 0 且分母为已知正数时应保留结果 0。
- 主表、详情、CSV 使用同一 `row.metrics` 结果与单位；缺失在页面留空并解释原因（可用“—”作非数值提示），CSV 留空，真实零保留 0。D0首交真实为0的CPA可按既有约定在页面/CSV显示文本“∞”，但 JSON 仍为null；缺失AF绝不能触发∞。CSV比率保留原始小数，单位在列说明中明确，避免重复乘100。
- GG 平台金额/CPA/coverage 为 `null` 时，审计卡仍展示可用的曝光、点击和 CTR，不能把整张卡渲染为空或显示假零。新页面兼容读取历史 schema 1；不可用字段不能补造成零。

### V2-REQ-05：增量兼容缓存与 CLI

- 保留 V1 Meta/TikTok 缓存表及列结构，不为 GG 的 null 语义改造 V1 的非空事实列；新增独立 GG 缓存表承载资产/平台事实、严格映射和历史 FX 证据。
- 从已核验V1 SQLite创建一致性副本；`data_root`仍为`/mnt/data-disk/opay-excellent-creatives`，V2缓存为其`cache/opay-excellent-creatives-v2.sqlite3`，影子输出为其`staging-public-v2`。V1旧缓存`cache/opay-excellent-creatives.sqlite3`不写入、不迁移、不清空；含WAL源库须用SQLite一致性备份，不可只复制主文件遗漏WAL。
- 快照以`snapshots/<month>/<stage>/<version>.json`新增版本文件，缩略图按内容散列新增，不覆盖V1；旧快照/媒体原地保留，供clone缓存中的绝对路径与preserved rows读取。不另建独立数据根，不扩大为迁移/复制全量历史媒体。
- 现有env未设置`OPAY_REPORT_CACHE_DB`时，V2代码自动选`-v2.sqlite3`默认名；原V1代码仍用旧文件名。只切`current`即可选对应默认缓存，不修改现有env、Nginx或timer/unit定义；如发现显式路径override，先核验并报告，不静默覆盖配置。
- 新增 `--clone-cache-from PATH`：从 PATH 克隆到 `--cache-db` 指定的新缓存；源不存在、源目标实际为同一文件、目标已存在或备份/完整性检查失败时拒绝，禁止覆盖既有数据。克隆失败不得启动回填。后续断点重跑使用已校验的 V2 缓存，不能再次克隆覆盖它。
- 新增 `--google-only`：必须与 `--refresh` 同用；只刷新独立 GG 事实，不重新读取/覆写 Meta/TikTok 平台、素材或 AF 事实，不改变既有选优结果。V2 月快照仍包含继承的 Meta/TikTok 和新增 GG 行，并为所有行补充 `metrics`。
- Google-only还要求冻结基线及其快照哈希有效；先比较Meta/TT事实/选优签名，再preserve旧行、基准和审计，只追加metrics。缺基线/快照损坏/签名变化均失败，`upgrade_audit`记录冻结来源及保留数量；不得重写fixture绕过保护。
- 历史终版冻结仍有效；包括刚克隆的已冻结月份，重算必须显式 `--rebuild`。`--google-only` 和 `--clone-cache-from` 都不是绕过冻结的开关。
- 首批回填固定 `2026-01` 至 `2026-07`，不带入未完成的 8 月。每月保留源缓存/代码/FX/映射版本、快照哈希、差异及 checkpoint；核对 Meta/TikTok 事实与既有业务字段不变，新增 GG 数量不得替代原渠道回归。

### V2-REQ-06：验收与发布门禁

1. 严格阈值、历史汇率切换/缺失、重复/歧义映射、null 与 0、六项公式及聚合、CLI 互斥/冻结用例有独立执行证据。
2. 1—7 月隔离缓存回填完整；Meta/TikTok 事实、选优、AF 及 V1 业务字段逐月无非预期变化；GG 均可精确回连真实 OPay 素材且只命中 B。
3. 桌面/390×844 移动端、证据详情、筛选排序、当前视图 CSV、Google 缺失值和平台 CTR 均通过；静态公开/旧报表/媒体契约不变。
4. GitHub-first：由发布负责人在对应阶段独立验证完成后提交/推送、确认精确提交，再由服务器fetch/checkout同一提交；本次执行者不commit/push、不访问或部署生产，服务器canary/正式验收单独记录。
5. 先备份 V1 release、env、缓存及公开清单，在隔离目录完成所有月份，再准备不可变版本文件及兼容前端；`latest.json` 作为最后的原子公开提交点，失败保留旧清单。
6. 回滚恢复上一成功 V1/V2 的 release、对应缓存配置和公开版本，不下线 V1 路由、不删除新旧事实/快照；具体顺序见 V2 部署章节。`sa-code-review.md` 与 `test-report.md` 仅待代码完成后由独立 QA 追加，本次不修改，也不借用 V1 通过结论。

### V2 变更记录

- 2026-08-27：增加 GG `type=3` video2/image4 严格素材事实，`type=0` 平台基准，micros/历史 FX、仅 B、AF/安装 null 和 schema 2 指标契约。
- 2026-08-27：确认采用不改 V1 Meta/TT 列的独立 GG 表方案；在复制缓存上 1—7 月回填，新增 `--google-only`、`--clone-cache-from PATH`，冻结仍需 `--rebuild`。当前状态为文档/实现交接，非测试或发布完成。
- 2026-08-27：稳定接口补充Campaign account-day缺失暂停B、preserve冻结签名、回归`--non-google-only`；数据根/旧快照媒体原地不变，V2默认`-v2.sqlite3`、影子`staging-public-v2`。实现方早期反馈51后端/34前端契约通过，非本文作者独立执行或生产发布结论。
- 2026-08-27：收口为全部source候选链合法一致；空FX候选跳过、历史USD缺失按缺口；conversions允许有限非负小数；补Campaign原币缺口，APM页面固定4位/CSV原精度。新增独立离线升级验收入口，范围及执行边界见dev-plan/deploy，正式提交发布仍由主线程负责。

## Google CPC / 图片视频 CTR 批准增量（2026-08-27，cpc_picvid_v1）

本节为最新批准口径，仅覆盖 Google；上文 V1/V2 的 B-only、Campaign CTR 用于 B、默认缓存名和相关验收记录保留为历史，不作为本增量验收结论。Meta/TikTok 的 AF CPA 规则 A、规则 B、原有指标/归因和其他报表/定时器均不变。

- GCP-REQ-01 / A：同月、同 App，按完整且精确映射的素材月 USD 消耗降序累计，阈值为**全量 Campaign 平台月 USD 总消耗的 50%**，包含跨线组及同消耗全部并列；并要求素材 CPC 严格小于平台 CPC。CPC = USD 消耗 / 点击，不能用 AF CPA、已映射消耗分母、已入选素材分母或舍入后的展示值替代。
- GCP-REQ-02 / A 暂停：平台 USD 未知/不完整、平台 USD 非正、平台点击为 0，或完整且精确映射素材消耗不足平台消耗的 50% 时，该月该 Google App 的 A 不可用，返回 `rule_a_unavailable_reason`。部分 USD 已知的素材不能算完整候选。A 暂停不连带暂停 B。
- GCP-REQ-03 / B：完整素材月 USD 严格 `>5000` 且素材 CTR 严格大于同月同 App 的全部图片/视频资产 `SUM(clicks)/SUM(impressions)`。基准来自 `ads_google_insights type=3 AND asset_type IN (2,4)`，包含未映射资产，不只取已映射/入选素材，不用 Campaign CTR，不平均每日/各素材 CTR。只有基准自身完整且可比较时才判定；不能因 Campaign USD 缺失否定完整的图片视频 CTR。
- GCP-REQ-04 / 展示与契约：A OR B，标记 A/B/A+B。页面按渠道说明公式，在 Google 详情显示素材/平台 CPC、图片视频 CTR 来源和 A 暂停原因；审计区并列保留 Campaign 参考 CTR 与图片视频整体 CTR。原25列表格、六项 metrics、31个 CSV 原列/顺序不变，CSV 只在末尾追加素材 CPC、平台 CPC、CTR 口径三列；Google conversions 仍只在详情，安装/AF 及依赖指标仍 null。
- GCP-REQ-05 / 映射修正：Google 视频允许精确链 `source_type=6 → ads_youtube_videos → 原 ads_source(source_type=3) → ads_custom_source.id`，保留原 direct-type3 路径。必须验证桥接 ID、App、视频枚举和全部候选链一致性；不把 YouTube 行 ID 当最终素材 ID，不允许一条好链掩盖坏链，不估算/分摊。
- GCP-REQ-06 / 隔离与发布：新默认缓存为 `<DATA_ROOT>/cache/opay-excellent-creatives-google-cpc.sqlite3`，保留旧 V1/V2 缓存、快照和媒体；路径优先级不变。从已核验 V2 基线一致性克隆后 Google-only 显式重建，Meta/TT 全字段/旧表哈希守恒。GitHub-first 精确提交、独立验证、完整版本文件先准备、`latest.json` 最后原子替换；所有可见月份须带 `selection_policy.google.version=cpc_picvid_v1`，失败保留旧公开版本。

当前交付仅前端、前端验证器及本目录 Markdown，未提交、未访问服务器。新规则全历史生成、YouTube 源库核验、独立 QA、浏览器实测及发布证据均待主任务补齐，不能沿用上文 V2 的通过数。
