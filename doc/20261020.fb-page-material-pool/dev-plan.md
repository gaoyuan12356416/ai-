# 实施计划

分支codex/fb-page-material-pool-20261020，基线5e43e24。

1. 领域后端：新版逐素材时间、具名/兼容迁移、创建人、池CAS/操作幂等、时点账本、原子任务冻结、跨槽顺序与模拟时钟测试。
2. UI：素材行选填时间、草稿转换、逐配对确认、创建人、Page池弹窗和分页操作；复用已有标准样式与素材选择器。
3. 身份/runtime：主API内部身份解析与创建人补全、gateway路由权限、sidecar组装、独立调度进程和systemd单元。
4. 独立复核与回归：旧核心/source/broker/gateway/UI测试，加新领域/身份/调度/浏览器验收；修复缺陷单。
5. GitHub-first：准确清单/源哈希、在线库备份迁移演练、排空后窄重启、真实Cookie非空读回、SHA和任务事实核对。
6. 回退使用本次manifest关闭新增调度和入口、恢复旧静态，保留新backend对现有v2/pool任务的兼容执行，不恢复库。

命令：python -m py_compile修改的Python；node --check修改JS；对应unittest与Node tests；git diff --check。
测试必须隔离SQLite和mock Graph，不用生产发布作为验收。
