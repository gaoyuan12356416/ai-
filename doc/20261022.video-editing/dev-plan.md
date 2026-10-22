# Implementation
1. Frontend: shared library, multipart upload, per-drama timeline/model/preview, autosave, tasks/results. Explicit demo mode makes no business writes.
2. Backend: SQLite, trusted owners, upload state, catalog/probe, frozen versions, one-flight coordinator.
3. GPU: deterministic recipe/render/probe, private COS output, mixed-type adapter, existing process recovery.
4. Integration: permissions/navigation, AST patch, systemd, exact provenance, backup/readback/rollback.

Checks: unittest/py_compile, Node syntax/model tests, browser interaction, FFmpeg probe and isolated real COS/GPU media. No social publication. Executed evidence belongs to test-report.md; secrets remain on the server.


## 2026-10-22 交互反馈修复

1. 独立分析生产探测账本与访问日志，区分真实媒体校验耗时和页面静默等待。
2. 增加动作忙态、单请求合并、目录会话和待添加操作归属保护；仅对所选待校验来源进行有界单飞轮询，自动续加。
3. 产品列表页内缓存并保留选择；保存成功后列表刷新不阻塞后续动作；项目取回后先呈现内容再受控补齐媒体元数据。
4. 修复测试harness的DOM/事件/JSON快照真实性，验证延迟、连点、乱序、关闭/切项目和原任务恢复。使用纯本地延迟后端验证真实浏览器反馈，生产验证以读取现有项目和目录为主。
5. GitHub精确提交、六份静态文件备份与原子替换、资源版本号更新，正式浏览器回读，保护全部API/worker/publisher进程与既有项目成片。
