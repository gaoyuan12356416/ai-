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
