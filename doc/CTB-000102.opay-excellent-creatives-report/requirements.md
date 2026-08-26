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

## 风险与待确认

- 已确认采用公开链接。`noindex` 不能阻止链接转发或未授权查看，这是接受的产品风险。
- Google 当前精确素材级数据契约不足，V1 允许 0 行但必须显示覆盖缺口；后续只有在仓库补齐精确 USD 素材指标和 AF 归因后才开放。
- 历史源文件可能失效，属单行降级，不影响绩效行保留。

## 变更记录

- 2026-08-26：用户确认仅统计 OPay；素材产品为 OPay，数据产品为 OPay NGN/OPay Pakistan。
- 2026-08-26：输出从飞书多维表格调整为 `ai.yingliangads.com` 新公开报表。
- 2026-08-26：创建人字段调整为素材制作者。
- 2026-08-26：用户批准本需求与实施方案，进入开发。
