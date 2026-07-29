# 部署文档

## 变更内容

X Post 短剧池增加账号粘性归属、确定性历史迁移、未绑定新剧校验失败 FIFO 顺延、事务级整批重校验、旧冻结队列发布前阻断和页面绑定账号展示。

## 2026-07-29 10:06 事件与修复边界

- 根因：旧路径在未绑定新剧发生确定性源数据、剧集或媒体预检失败后，直接将 10:06 批次结束为 `failed_preflight`；它没有先把该无历史新剧排除并继续扫描后续 FIFO 候选，因此即使池内仍有可用新剧也无法凑齐全部账号。
- 修复只允许“未绑定且无任何队列/发布历史”的失败新剧进入 `validation_failed` 并 FIFO 顺延。
- 已绑定或有历史的短剧仍是 fail-closed：进入 `needs_review`、保留归属、整批停止，禁止静默换剧。
- runner 必须先凑齐全部冻结账号的合格候选，再由 sidecar 在单事务中写入全部队列与新归属；不足时 `failed_preflight`、零队列、零新增归属。
- 本变更只保障部署后的自然发布时间点，不补跑 10:06，也不会自动创建 Post。
- 代码评审已否决失败批次恢复接口：它缺少管理员授权和不可变审计，并可能绕过已停用配置。如需补发，必须另立需求完成上述安全能力后再评审。

## 配置项

无新增环境变量。继续使用既有 sidecar、SQLite、MySQL 只读连接、定时器和媒体存储配置。

## 数据库变更

- 表：`x_post_drama_pool` 新增 3 列。
- 索引：`ux_x_post_drama_pool_active_account`、`idx_x_post_drama_pool_assignment`。
- 触发器：队列账号归属、绑定不可变、绑定证据、插入证据和绑定依据队列防删除。
- 迁移会扫描既有短剧队列/日志并回填归属，不改历史行。

## 部署前门禁

1. 当前时间不在发布点前后 90 秒；09:55 后不得在次日 10:00/10:06 批次前升级。
2. 短剧 schedule run 不存在 `claimed/queued/running/needs_review`；队列不存在 `publishing` 或 unknown。
3. 在数据盘创建 SQLite 在线备份；禁止把备份写到 92% 使用率的根盘。
4. 对备份副本运行新代码迁移和 `PRAGMA integrity_check`。
5. 演练断言：
   - pool/queue/log 计数迁移前后相同；
   - `bURak9Oyn7` 绑定账号 10、依据队列 35、下一集 8；
   - 既有 Episode 1–7 账号映射不变；
   - 池53 `3CRScaBEY0` 和池54 `zuMg6fyfSs` 均无绑定、无队列/发布历史，真实媒体预检均为时长超出X合同；按新合同校正为 `validation_failed`；
   - 校正后的候选预览为 10→池2/E8、9→池57/E1、8→池60/E1、7→池131/E1、6→池132/E1；账号5缺少候选；
   - 在管理员新增至少1部合规短剧前，6账号批次必须 `failed_preflight`、零队列、零部分发布。
6. 只读确认 10:06 原批次保持失败状态且没有被人工恢复、补建队列或补发；发现任何额外写入立即停止部署并核查。

## 部署步骤

1. 将已推送 GitHub 的精确 commit 检出到数据盘不可变 release 目录。
2. 安装/校验 release 文件，不在服务器工作树直接编辑。
3. 停止共享 schedule claim/worker timers，并确认没有运行中的 schedule service。
4. 切换 `/opt/x-post-automation/current` 到新 release。
5. 同步主后端需要的 service/static 文件。
6. 重启 `x-post-automation.service` 与主 API 服务；不手工触发定时发布。
7. 使用新 `/internal/posts/drama-pool/check` 精确提交池53和池54各自的 `source_not_repairable` 脱敏错误；入口必须再次断言两剧均未绑定且无任何队列历史，结果必须为 `updated_count=2`。
8. 只读确认池53、54为 `validation_failed`，10:06 原批次仍为 `failed_preflight` 且队列/日志均为 0。
9. 用部署代码在生产在线备份副本上验证新候选映射和完整媒体预检；当前结果应明确为5个可用映射、账号5缺候选，生产库不建计划、不发帖。
10. 恢复 timer 原计划，由下一正常发布时间点执行。

## 验证步骤

- 服务与 timers 为 active，启动日志无 schema 错误。
- 生产 SQLite `integrity_check=ok`，表/队列/日志计数不变。
- 池53仅发生 `needs_review`→`validation_failed`，池54仅发生 `pending`→`validation_failed`；两者仍无绑定、无队列和发布历史。
- 短剧池页面显示 `bURak9Oyn7` 绑定账号 10；其余可用未绑定剧显示“待分配”，不可用剧显示“不可分配”，已完成剧显示“历史发布账号”。
- 用部署代码只读调用候选分配，结果与部署前演练断言完全一致。
- 下一自然批次完成后检查：同一 `content_id` 的新队列只有一个账号，且池归属、队列、日志三者一致。
- 构造未绑定无历史的坏剧位于好剧之前：坏剧写入 `validation_failed`，下一剧按 FIFO 补位；最终计划仍覆盖全部冻结账号。
- 构造已绑定或有历史坏剧：该剧进入 `needs_review`，整批停止且不换剧。
- 构造无法凑齐全部账号：批次为 `failed_preflight`，该批次队列数和本次新增归属数均为 0。
- 检查 10:06 原失败批次在部署前后均未被恢复或补发；只验证下一自然发布时间点使用新逻辑。

## 回滚方案

1. 先停止共享 schedule claim/worker timers，防止旧 runner 在新 schema 上继续选剧。
2. 回切到上一不可变 release 并重启 API/sidecar。
3. 保留升级后的 SQLite 数据库及在线备份，不恢复旧数据库、不删除归属字段。
4. 在兼容修复重新上线前保持短剧自动发布关闭；旧代码不允许带着启用的短剧 schedule 运行。

## 注意事项

- 本次部署不补发、不创建测试 Post。
- timer、catch-up、启动脚本、内部接口和数据库操作均不得追赶 10:06。
- 当前池缺少1部合规新剧；不得自动从源库补池、缩减账号范围、跳过Episode 1或放宽140秒合同。由管理员明确新增短剧后，才能满足“1部续发+5部新剧”的6账号批次。
- 若业务仍需补发 10:06，必须另立需求，加入管理员审批、不可变审计、停用配置门禁和排重/结果核对；本部署不得夹带实现。
- 数据库迁移失败必须原事务回滚并停止部署。
- release commit 与实际服务器文件哈希必须可追溯。

## 2026-07-29 生产发布记录

- GitHub/生产 release：`569640e8ab737aaf720d2cfc1e7c7978a14d24dd`。
- 停机点在线备份：`/mnt/data-disk/x-post-automation/backups/20260729T145013+0800-drama-preflight-fallback-final-569640e`。
- 运行目录：`/opt/x-post-automation/current` → `/mnt/data-disk/x-post-automation/releases/569640e8ab737aaf720d2cfc1e7c7978a14d24dd`。
- sidecar 与主 API 的 `features/x_posts/service.py` SHA-256 均为 `585872cbfcf2555b161a0f9013cf463a1bf9c605cefa8e73fdae7680abc62557`。
- 池53、54的内部校验更新数为2；除两行允许的状态/错误/时间字段外，schedule config/run、queue、publish log、daily/catchup run 和 material pool 与停机点备份逐行一致。
- 10:06原批次仍为 `failed_preflight`、0队列、0日志；没有补发。
- Token哈希/权限清单未变化；SQLite `integrity_check=ok`；公网短剧池页和X health均为HTTP 200。
- `x-post-schedule-claim.timer`、`x-post-schedule.timer` 已恢复；worker自然轮询返回 `no_due`。
- 当前只读映射为账号10→池2/E8、9→池57/E1、8→池60/E1、7→池131/E1、6→池132/E1；账号5无候选。管理员新增至少1部合规新剧前，整批保持零发布。
