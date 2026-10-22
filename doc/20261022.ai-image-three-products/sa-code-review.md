# SA 代码评审

2026-10-22：已独立评审三产品 collector/analysis、前端失败状态、冻结发布/恢复/回滚及独立状态部署。当前 Python 全套 115 项通过；独立发现的回滚 health 错位已经修复并回归通过。CPU 准备阶段真实原始数据已经独立审计通过；后续公网发布与独立浏览器验收均完成，见本文末尾与 test-report.md。

最终评审范围：应用映射 fail closed、正式 ID 来源归属、日/周指标与分组守恒、逐日同范围对照、静态资源 manifest 发布顺序、gzip/缓存、缩略图边界及旧报告兼容。

| 编号 | 级别 | 位置/风险 | 要求 | 状态 |
| --- | --- | --- | --- | --- |
| CR-01 | P1 | 旧 pool 查询只有正式 ID，无应用维度 | 新版 pool/verified_pool 按 app 与 channel 独立保留，未确认行按真实应用分配 | 已修复；总额抵消但 cell 错误 fixture 仍阻断 |
| CR-02 | P1 | 旧 analysis 只分 content_id | 三产品剧目 key 需含来源产品 | 已修复；跨产品同 ID 剧目 fixture 通过 |
| CR-03 | P1 | 旧页面包含全量明细 | 新版首屏不得嵌全量数据或自动请求原图 | 模板/Node fixture、真实数据桌面手机及三次公网冷启动均通过，0明细/原图首屏请求 |
| CR-04 | P2 | `restore_public` 仅恢复内容，health 仍标记新版成功 | 独立备份/恢复先前 health，内容漂移保护与日常 failed health 分开 | 已修复；旧 v3 首页与成功 health 同步恢复，首装保留新页 health，两回归通过 |
| CR-05 | P2 | 缩略图前 25 曾按来源产品总消耗选取 | 使用实际投放 view 的前 25，再合并本来源产品昨日新增 | 已修复；跨产品 top 25 测试通过 |

复现 CR-04：内存 FakePublisher 先放 2026-10-20 首页及成功 health，再发布 2026-10-21，调用 `restore_public`。实际恢复正文为 10/20，health 仍为 `published/report_date=2026-10-21/version=v1`。前端据此提示存在新数据，但最新入口仍是已恢复的旧页。未访问或修改生产。

修复复核：`backup_mutable` 保存旧 v3 首页对应的 `previous_success`；失败 health 从 last_success 提取日期、生成时间和版本；回滚完成后同步成功 health。health 不进入内容漂移条件，未知发布期间更新失败状态不会阻止恢复。首次升级无旧 v3 首页时新页被保留，继续保留其 health。独立重跑两新增场景及全套通过，CR-04 关闭（BUG-002）。

验证记录：

- `python -B -m unittest discover -s ops/ai-image-daily -p '*test*.py' -q`：115 项通过（55 个旧数据/账本用例、32 个三产品用例、20 个发布/缩略图用例、8 个 UI 用例）。旧版四个发布 fixture 已迁移为 v3 发布测试。
- `compile()` 遍历模块 Python 源码：通过；`git diff --check`：无错误。
- 发布故障测试实际覆盖相同文件恢复且不重查、不重传已存在对象、manifest/raw/gzip 篡改拒绝、不可变对象冲突写前失败、外部首页 metadata 漂移拒绝、health 失败不破坏 frozen recovery、回滚前所有对象预检查、旧快照目录写入拒绝。
- 前端 Node fixture 验证失败 health 异步显示、细节加载失败重试、产品/周切换、筛选 CSV、无原图自动请求；不能替代真实 Chrome/手机宽度及公网冷启动测量。

部署静态结论：唯一 service/timer 沿用，状态改为 `/mnt/data-disk/ai-image-daily-v3`；精确 Git commit 校验与代码/units 备份沿用；源 SQLite 只读保护保留；部署器不主动启动 timer。尚未以此文档声明生产发布成功。

## CPU 准备产物独立审计

证据目录 `D:\reports\ai-image-three-products-20261022`；详细结果为 `independent-audit.json`，记录全部输入 SHA256、33 个变化分组的指标差额与原始更新时间。审计读取 `server-report.json`、`facts.json`、`lineage.json`、`old-facts.json`、`old-report.json`；未导入 report analysis 计算函数，使用独立 Decimal 汇总。

- 数据日 2026-10-21，完整区间 10/15–10/21；生产报告自带 921 项校验通过、未知有消耗素材 0。独立验证 296 项基础指标/去重/来源产出/池守恒，另 288 项首入原图、迁移、历史补传、覆盖剧目、逐日匹配其他图片及服务端实际结果一致性，共 584 项通过。
- 应用清单冻结为 DramaWave 148 个、MoboShort 17 个、MoboReels 4 个（含各 root）；排除 root 1479 下 `dramawaveminis`（3346）。正式素材截至日报日 5,744 张，昨日新增 60 张；分来源产品为 5,552/90/102 张及新增 36/12/12 张。
- 昨日三产品消耗/AF D0 收入分别为 DramaWave **352.69 / 224.77 USD**、MoboShort **568.39 / 255.00 USD**、MoboReels **101.46 / 0.32 USD**；总计 **1,022.54 / 480.09 USD**。所有字段按实际投放 app 归属后与服务端日/周汇总吻合。
- 与旧 DramaWave 日报核对：正式 ID 集合完全相同，均 5,552；近七天 `(dt,id,app,platform,optimizer,product)` 原始分组键均 654 个，新增/删除键均 0；实际投放应用同为 `[w2a]drama-double` 与 `[w2a]waveads`，旧 AI 事实中也没有 Minis 行。
- 仅 10/21 的 33 个既有分组更新；旧 updated_at 约 09:00–09:02，本次约 10:35–10:40。昨日旧值 350.73/224.57、本次 352.69/224.77；差额为消耗 +1.96、收入 +0.20、曝光 +279、点击 +19、安装 +4、AF 安装 +3、付费不变。差异已存在于后读的上游日表原始行，属于投放及归因数据更新，未发现范围漂移或计算变化；不能仅称归因回补，因为消耗也更新。
- 冻结 manifest 版本 `20261021-7080be0e7235fc18` 共 123 个内容对象，`report_sha256` 与本地服务端原始报告文件精确一致。首屏 HTML gzip 24,113 bytes，必要 JS gzip 12,439 bytes，共 **36,552 bytes（35.70 KiB）**，低于 150 KiB；这是冻结包大小，不代表网络加载时延。缩略图按产品选取 61/34/35 张，共 130 张解析成功、114 个内容去重文件、失败 0，阶段耗时 48.217 秒。

准备阶段结论：真实只读准备数据可进入发布验收；独立原始数据审计本身不替代 COS 或浏览器验证。

## 发布后验收补记

运行版本 `60c4e7c02da1db0ecfddb4feea31ce8a162ed0c2` 已于 10:52:49 CST 发布，123 对象与 health 全部公网回读验证，旧四份历史页及旧 state 保持原值。唯一 timer 已恢复，下次 10/23 09:00 CST，同日重复触发跳过成功。证据 `server-acceptance.json` 与 `publish-receipt.json`。

独立浏览器工作流使用三个无缓存上下文测得汇总可见时间 1.0381、1.2086、0.9207 秒，中位 1.0381 秒；首屏明细/图片请求均 0。真实产品/日周、CSV、日期导航、旧入口 hash、390px 手机均通过。原始网络和截图见 `browser/public-acceptance-evidence.json`。所有 P1/P2 评审项已关闭，最终完整验收见 `test-report.md`。
