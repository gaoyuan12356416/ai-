# 测试报告

## 测试结论

业务日汇总与 partial 状态拆分已随功能提交 `d4af68af83e55b4df65fc13f273738ba98dfe189` 推送 GitHub 并部署到生产。本地与服务器 checkout 均为 59/59 unittest，通过 Python compile、`node --check` 与 `git diff --check`；生产服务、静态资源、鉴权和线上只读聚合数据验证通过。

真实登录态 UI 视觉验收尚未完成：服务重启后 Chrome 与 IAB 的既有登录态均已失效，需重新登录后确认日卡、状态文案与批次展开。不得把该项写成已完成。

## 测试范围

执行批次、限流熔断、Graph安全门、续跑状态、MySQL日志适配、业务日只读聚合、partial 状态 reducer、daily/raw API、日志 UI、静态缓存与共享 app 窄补丁。

## 执行统计

| 验证项 | 本地 checkout | 服务器 checkout / 生产 | 结论 |
| --- | --- | --- | --- |
| Python unittest | 59/59 | 59/59 | 通过 |
| Python compile | 通过 | 通过 | 通过 |
| JavaScript `node --check` | 通过 | 通过 | 通过 |
| `git diff --check` | 通过 | 通过 | 通过 |
| 服务/静态页/鉴权 | - | active、NRestarts=0、HTML 200、未登录 API 401 | 通过 |
| 业务日只读聚合 | 单元与生产同源验证通过 | 7月15日完成、7月14日限流后未完成 | 通过 |
| 真实登录态 UI 视觉验收 | - | Chrome/IAB 登录态失效 | 待重新登录 |

## 缺陷情况

BUG-002、BUG-003 已完成代码修复并部署，服务端只读数据符合预期；两个缺陷均等待真实登录态 UI 视觉验收后关闭。

## 验证证据

- 功能提交：`d4af68af83e55b4df65fc13f273738ba98dfe189`，已推送 GitHub。
- 本地与服务器 checkout：59/59 unittest；Python compile、`node --check static/ad-control-pages.js`、`git diff --check` 全部通过。
- 二次部署备份：`/root/drama_material_service/backups/ad-control-daily-log-20260715-160140`，包含 service、static 与 published_static 回滚副本。
- 生产文件 SHA256 前缀：app `c5f5be0f...`、service `bfe65db4...`、JS `84c9f35d...`、CSS `1f7265e...`。
- `drama-material-api.service` 为 active，NRestarts=0，验证时主机负载保持低位。
- 公网 HTML 返回200，页面引用 cache buster `20260715log2`；未登录日志 API 返回预期401。
- 线上只读聚合：2026-07-15 为2批、386次尝试、355成功、31跳过、0错误、remaining=0，状态“当日执行完成”。
- 线上只读聚合：2026-07-14 为4批、800次尝试、746成功、53跳过、1错误、remaining=19，状态“限流后未完成”。
- 本次二次部署未触碰 runner、未调用 Meta 写接口、未修改 DB 数据。
- `43.166.187.96 -> 101.32.56.53:63353`：`@@read_only=0`，`CURRENT_USER()=ads_aius@43.166.187.96`。
- 随机探针表完成 CREATE、单行 INSERT、UPDATE、SELECT、DELETE、DROP；操作全部成功，探针表已确认无残留。
- 运行时代码负向门禁：错误端点、错误库表、超512KiB、第三个突发写均在连接数据库前失败；读只走63350，写只走63353，运行时无DDL/DELETE。
- 首次日志写入回退SQLite时，runner不会立即再发状态UPDATE；仅`log_store=ads_ai`时允许第二条受控状态写入。
- API与runner的live preview跨账户并发均默认4，生产补丁会把旧默认12收紧为4。
- 正式表已在 `63353` 创建并从 `63350` 回读：31列、5个索引、InnoDB；上线单行写/更新/读取/清理探针通过且0残留。
- 首次 action-log 基线源码为 `82fdab6a5c88565a45d1e7d2ac2a9dddf9bb3dce`，备份目录为 `/root/drama_material_service/backups/ad-control-execution-log-20260715-130314`；本次业务日优化使用上述 `d4af68af...` 提交和独立二次备份。
- SQLite与MySQL均有16个 action_id 且集合完全相同；同批迁移二次执行后仍为16行。
- 首次 action-log 基线上线时曾完成登录态16张原始日志卡和186/186详情验证；该证据不能替代本次 `log2` 日汇总 UI 的重新登录视觉验收。
- 首次部署因静态页健康检查使用了错误的后端直连路径而自动回滚；文件、环境与服务恢复均经校验，修正检查路径后的最终部署通过。

## 遗留风险

- 正式Meta pause不作为上线首测；先做日志读、preview/dry-run。
- `results_json` 后续需制定180天归档策略。
- 后续共享生产app部署仍必须对真实文件做备份和diff门禁。
- 当前 `ads_aius` 在 `ads_ai` 内仍有宽权限；应用和 skill 门禁不能替代未来由 DBA 创建的最小权限专用写账号。
- Chrome/IAB 需重新登录后完成 `log2` 日卡、状态标签、批次列表和逐批懒加载的视觉验收。

## 发布建议

本次业务日汇总代码与静态资源已部署，服务端与公网只读检查通过。真实登录 UI 视觉验收完成前，不关闭 BUG-002/BUG-003，也不宣称本次前端验收全部完成。正式 Meta pause 不作为该展示改动的验证手段。
