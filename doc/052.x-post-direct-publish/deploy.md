# 部署文档

## 变更内容

发布 scheduler、X post store 和 OAuth sidecar 的同一 GitHub commit；素材/短剧 schedule 改为 deferred-at-publish。

## 配置项

无新增 secret/env。保留现有媒体大小、host、timeout、Premium Relay、账号和 storage 配置。

## 数据库变更

Sidecar 启动时对 `x_post_queue` 幂等增加 `media_validation_mode TEXT NOT NULL DEFAULT 'preflight'`。部署前必须在线备份 SQLite/WAL/SHM；不从旧备份覆盖新运行数据。

## 部署步骤

1. 以 GitHub commit 为唯一代码来源，确认本地/远端 commit 一致。
2. 再次检查 run 271 为 claimed 且 queue/log/attempt/unknown 全 0；若不满足立即停止部署并审计。
3. 停止 schedule/manual/claim 等相关 timers，等待活跃 oneshot 清零；当前旧 schedule 已在保护条件下停止。
4. 备份主 X SQLite、账号 SQLite、Token 哈希/权限清单、units/env 哈希和 active release 指针。
5. 建立新的不可变 release，切换 `/opt/x-post-automation/current`；同步主 API 使用的 `features/x_posts/service.py`，不得同步数据库或 Token。
6. 重启 Sidecar 和必要的主 API，验证 health、schema、quick_check/FK、真实 endpoint。
7. 恢复 claim/schedule/manual timers，手工启动一次 schedule service 仅续跑已认领 run 271。
8. 观察 queue/log/run/repost ledger 直至终态；不得创建额外测试 Post。

## 验证步骤

- run 271 复用原 slot/config，不出现第二个相同时间点 run。
- 建队延迟从几十分钟降为轻量元数据阶段；queue 的模式为 deferred、指纹为空/size0。
- 每条成功有 Post ID；Relay 同时有 source Post 与 target Repost 终态。
- known failure 后后续 queue 仍有 attempt；unknown/429 后无后续 attempt。
- SQLite `quick_check=ok`、FK=0、无 queued/publishing/unknown 残留（若业务仍在执行则按状态解释）。
- timers active/waiting、Sidecar/main health 正常，Token 内容/权限未变化。

## 回滚方案

1. 停止所有 X 发布 timers 与 oneshot。
2. 若尚未创建 deferred queue，可直接把代码指针切回前一 immutable release 并重启服务。
3. 若已存在 deferred queue，不允许让旧代码处理：保持新发布层直至这些队列全部终态，或人工冻结并审计后再回滚。
4. 回滚只切代码和 unit；绝不恢复旧 SQLite/Token 备份覆盖更新后的账本。
5. 复核 health、schema、账本和 timers。

## 注意事项

- 15:11 旧进程于 16:00 在 0 queue/log/X attempt/unknown 保护条件下被终止；schedule timer 暂停，claim timer保留认领。
- 16:04 后可能还有 pending drama run；部署前按相同幂等/未知门禁冻结清单并由新 runner 顺序处理。
- 不打印 env/Token 内容，不执行额外真实 X canary。
