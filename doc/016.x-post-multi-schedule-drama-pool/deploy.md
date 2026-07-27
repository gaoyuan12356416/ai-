# 部署文档

## 变更内容

- AI 后台增加素材池多账号/多时间配置和 Post 短剧池。
- sidecar 增加排期、短剧池和冻结计划接口。
- 增加 claim/worker 两个 systemd timer，替代固定 10:00 的旧 daily timer。
- nginx 为两个管理页面增加禁止缓存。

## 配置项

- 新建 `/etc/x-post-schedule.env`，root:root `0400`。
- 从 `deploy/x-post-schedule.env.example` 复制非秘密配置。
- 继续复用 `/etc/x-post-daily.env` 的 daily bearer、只读 MySQL、媒体白名单和工作目录。
- 不在新文件保存 OAuth/X token。

## 数据库变更

- sidecar 启动时通过 `ensure_storage` 增量创建配置、短剧池、批次表并扩展队列。
- 部署前对 SQLite 执行 `.backup`、`PRAGMA integrity_check` 和 schema/count 清单。
- 部署后再次执行完整性检查并核对旧 run/queue/log/pool 数量没有减少。

## 部署步骤

1. 完成测试、合并生产 sidecar `f8389fe` 与主 API TT source-cache `efa8652`、提交并推送 GitHub 分支；服务器仅部署该远端 commit。
2. 记录当前 release symlink/commit、systemd 状态和文件 hash。
3. 备份 SQLite、环境文件权限/hash、nginx 配置和线上 `navigation.json`。
4. 创建不可变 release，安装 Python/静态文件和新的 systemd unit。
5. 迁移 SQLite 后重启 sidecar/api，先验证 loopback health 和内部只读查询。
6. 按 key 合并 `xPostDramaPool` 导航项；保留线上其他导航及 `adminOnly` 人工设置。
7. nginx `-t` 成功后 reload。
8. 用现有素材排期初始化新 material config（原账号集合、原 10:00），短剧排期保持 disabled。
9. `systemctl disable --now x-post-daily.timer` 并 mask；确认不会再触发。
10. enable/start `x-post-schedule-claim.timer` 和 `x-post-schedule.timer`，不手工启动发布 service。
11. 核对 timer 下次自然触发、服务权限、共享锁、数据盘和日志。

## 验证步骤

- 页面：
  - `https://ai.yingliangads.com/x-post-material-pool.html`
  - `https://ai.yingliangads.com/x-post-drama-pool.html`
- 用管理员和有 `x_accounts` 权限的非管理员分别验证动态可见性。
- 浏览器保存/刷新多个账号和时间；不要选择已到点的当前窗口。
- 加入一个已知短剧 ID，只验证预览、入池和剧集明细；短剧排期未启用时不得创建 Post。
- 只读核对：
  - 旧 timer masked，新 timers active；
  - schedule config 与页面一致；
  - SQLite integrity `ok`；
  - HTML 响应 `no-cache, no-store`；
  - 没有未预期的新 queue/log/Post。

## 回滚方案

1. 停用新两个 timer，阻止新认领。
2. 若本次上线后没有任何发布写入，可切回旧 release 和部署前 SQLite 备份。
3. 若已产生 queue/log/Post，严禁恢复旧 SQLite；只回滚代码，保留当前数据库审计事实。
4. 恢复 nginx/navigation 备份并 reload。
5. 是否恢复旧 daily timer 必须人工决定；不得和新 timer 同时启用。

## 注意事项

- 发布部署不主动补发、不手工触发自然时间点。
- 不覆盖线上整份导航配置。
- 不打印 token、env 内容、完整 Post body 或带参数长链。
- 生产证据和最终 commit 在部署完成后补入本文件。
