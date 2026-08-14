# 部署文档

## 变更内容

授权/Access Token 状态分离、素材/短剧/人工排期预检刷新、X Auto 真实任务刷新、最终写入保护及两套账号 UI。

## 配置项

无新增配置；复用现有 X OAuth Client、Sidecar、daily/auto internal bearer 与 Token 目录。

## 数据库变更

无 schema 或数据迁移。历史账号、Run、Task、Queue、Log、Post 不重写。

## 部署步骤

1. 推送 `codex/x-offline-token-refresh-20260814` 精确提交。
2. 校验生产基线 release `7ed5f203e028129f88afbc675da9237326bfd364` 与 Git 文件哈希一致。
3. 在线备份 X/X Auto SQLite、Token 目录、代码、两处静态页、units/env 元数据和定时器状态。
4. 从 GitHub 精确提交创建不可变 release，在 release 内跑编译与聚焦测试。
5. 停止 Sidecar/X Auto，原子切换 `/opt/x-post-automation/current`，同步主应用静态和 Nginx docroot。
6. 启动并健康检查 Sidecar/X Auto；主 API 代码未变则不重启。
7. 验收后按部署前 enabled/active 状态恢复 timers/path。

## 验证步骤

- Sidecar `/health`、X Auto health、主 API/Nginx 页面返回 200。
- 安全账号 DTO：部署后 17 个账号均为 `status=active`、`authorization_refreshable=true`、`publish_eligible=true`；自然调度后 2 个为 `valid`，其余 15 个为 `expired_refreshable`，不输出 Token。
- 页面文案与静态哈希一致，匿名写接口仍 401/403。
- 两个 SQLite `quick_check=ok`，FK 无异常。
- Queue/Log/Published/unknown、X Auto Run/Task/Ledger 与部署前一致。
- Token 数量、mode/owner/组合哈希在未发生自然刷新前一致；若自然刷新发生，只接受审计到的预期轮换，不恢复旧备份。

## 回滚方案

停止发布 timers/path 与受影响服务，切回 `7ed5f20`，恢复两处静态页和 unit 文件，重启 Sidecar/X Auto，再按原状态恢复触发器。默认保留当前 SQLite 与 Token；刷新发生后严禁用备份 Token 覆盖。

## 注意事项

不运行 `run-now`、不手动启动发布 oneshot、不创建 canary Post；验证依赖 mock/离线、安全 DTO 和自然 timer 结果。

## 实际部署记录

- GitHub 分支：`codex/x-offline-token-refresh-20260814`。
- 生产代码提交与不可变 release：`ddc6c0b09a5040d9b024ff1532b81c79698f4945`，路径 `/mnt/data-disk/x-post-automation/releases/ddc6c0b09a5040d9b024ff1532b81c79698f4945`。
- 完整回滚包：`/mnt/data-disk/x-post-automation/backups/20260814T183516+0800-x-offline-token-refresh-ddc6c0b`；manifest、两份 SQLite `quick_check` 和 FK 检查均通过。
- 切换后 Sidecar、X Auto、主 API 均为 `active`，Nginx 配置检查通过；五个 timer/path 按部署前状态恢复，`x-post-daily.timer` 保持 masked。
- 部署前后主 X 账本保持 Queue/Log/Published/unknown=`254/244/242/0`，未用真实 Post 验证。
- 18:45 自然 X Auto 调度先通过 Sidecar 刷新账号 19、20；17 份 Token 中仅 2 份发生预期轮换，15 份未变，全部保持 `0600` 与 `x-post-automation` 属主。
- 18:46 自然 runner 处理 Run 10 的两个任务，均以 `x_auto_no_eligible_material` 结束；Run 为 `completed`，没有非空 Queue ID、Log ID、Post ID 或 Post URL，X Auto Ledger 仍为 4。
- 维护窗口错过的 18:22 素材时点由既有 grace/idempotency 规则自然忽略，未人工补发。
- 部署脚本曾在停止服务前两次安全失败：一次发现 `/root/drama_material_service` 不是 Git 仓库，一次发现主运行时没有 daily runner；两次均未切换 release、未停止服务、未改变账本。最终改由 GitHub 精确提交构建 release 并成功切换。
