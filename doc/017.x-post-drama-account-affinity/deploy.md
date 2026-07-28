# 部署文档

## 变更内容

X Post 短剧池增加账号粘性归属、确定性历史迁移、事务级重校验、旧冻结队列发布前阻断和页面绑定账号展示。

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
   - 新分配预览为 10→池2/E8、9→池53/E1、8→池54/E1、7→池57/E1、6→池60/E1、5→池131/E1；
   - 池132 保持未绑定。

## 部署步骤

1. 将已推送 GitHub 的精确 commit 检出到数据盘不可变 release 目录。
2. 安装/校验 release 文件，不在服务器工作树直接编辑。
3. 切换 `/opt/x-post-automation/current` 到新 release。
4. 同步主后端需要的 service/static 文件。
5. 重启 `x-post-automation.service` 与主 API 服务；不手工触发定时发布。
6. 保持定时器原计划，由下一正常发布时间点执行。

## 验证步骤

- 服务与 timers 为 active，启动日志无 schema 错误。
- 生产 SQLite `integrity_check=ok`，表/队列/日志计数不变。
- 短剧池页面显示 `bURak9Oyn7` 绑定账号 10；其余可用未绑定剧显示“待分配”，不可用剧显示“不可分配”，已完成剧显示“历史发布账号”。
- 用部署代码只读调用候选分配，结果与部署前演练断言完全一致。
- 下一自然批次完成后检查：同一 `content_id` 的新队列只有一个账号，且池归属、队列、日志三者一致。

## 回滚方案

1. 先停止共享 schedule claim/worker timers，防止旧 runner 在新 schema 上继续选剧。
2. 回切到上一不可变 release 并重启 API/sidecar。
3. 保留升级后的 SQLite 数据库及在线备份，不恢复旧数据库、不删除归属字段。
4. 在兼容修复重新上线前保持短剧自动发布关闭；旧代码不允许带着启用的短剧 schedule 运行。

## 注意事项

- 本次部署不补发、不创建测试 Post。
- 数据库迁移失败必须原事务回滚并停止部署。
- release commit 与实际服务器文件哈希必须可追溯。
