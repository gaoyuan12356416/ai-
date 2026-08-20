# 部署文档

## 变更内容

- CPU X schedule runner、X Post store、Drama selector 和 OAuth Sidecar 代码。
- 恢复 `x-auto-post-metric.timer`，刷新自然指标窗口。

## 配置项

无新增环境变量。保留生产当前 `.env`、Token store、SQLite 和 systemd unit。

## 数据库变更

无 DDL。部署前在线备份主 X Post SQLite 与 X Auto SQLite；回滚不恢复旧数据库。

## 部署步骤

1. 将已通过测试的精确 Git 提交推送 GitHub。
2. 记录六个 X timer、在途 oneshot、当前 release、unit 和非秘密环境哈希。
3. 在线备份 SQLite，验证备份 `quick_check=ok` 和外键为 0。
4. 在不跨越临近自然发布点的窗口暂停触发 timer，等待在途任务退出。
5. 从 GitHub 精确提交创建不可变 release，原子切换 `current`。
6. 同步主 API 所需模块，重启 OAuth Sidecar/主 API；只重启受影响服务。
7. 恢复原 timer，并显式启用 `x-auto-post-metric.timer`。

## 验证步骤

- 精确 commit、文件 SHA、服务健康、timer enabled/active。
- 服务器完整 X 回归和生产 SQLite 副本部分容量演练。
- 主库/X Auto 库 `quick_check=ok`、外键 0、unknown 0。
- 观察自然 schedule/claim/X Auto timer；不创建非计划测试 Post。

## 回滚方案

1. 暂停相同 timer 并等待在途任务退出。
2. 将 `current` 原子切回部署前 release，恢复备份 unit/非秘密配置。
3. 重启受影响服务并恢复 timer 状态。
4. 保留当前 SQLite、Token、队列和发布账本，禁止用旧备份覆盖新事实。

## 注意事项

- 若临近自然排程点，不执行切换，先让当前版本完成该时间点。
- 回滚代码不会撤销已经正常发布的部分队列。
- 部署记录、commit、备份和自然验收结果完成后补录本文件。

## 2026-08-20 生产记录

- GitHub 代码提交：`10bb243bd3c12bdeac10a4a922829c8c96088f21`。
- 当前不可变 release：`/mnt/data-disk/x-post-automation/releases/10bb243bd3c12bdeac10a4a922829c8c96088f21`。
- 回滚 release：`/mnt/data-disk/x-post-automation/releases/9bbd5cad336be2d9c21002778969ceab0b16db0f`。
- 备份目录：`/mnt/data-disk/x-post-automation/backups/20260820T105330+0800-partial-capacity-10bb243bd3c12bdeac10a4a922829c8c96088f21`。
- 精确 Git blob 校验通过；主 API 与 Sidecar 的 `service.py`、`drama_selector.py` 内容一致。
- `x-auto-post-metric.timer` 已恢复为 enabled/active，指标代次 1185 已 ready。
- 部署后 OAuth Sidecar、X Auto Sidecar、Drama Material API active；六个 X timer active；8810/18833 健康检查通过，8787 正常监听。
- 在线两套 SQLite 均 `quick_check=ok`、外键异常 0、unknown 0。回滚只切代码并恢复模块，不回滚 SQLite/Token/账本。
- 首次 18833 探针早于监听就绪，得到一次 connection refused；服务随后正常监听并通过重试健康检查，日志无启动异常，未触发回滚。
