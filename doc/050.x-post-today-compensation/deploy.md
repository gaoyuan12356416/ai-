# 部署文档

## 变更内容

CPU immutable release 增加 drama 同日范围补偿能力、范围补偿子 run 零写恢复审计链，以及素材语种 FIFO 一致性修复；不修改 GPU 代码。

## 配置项

生产 `/etc/x-post-schedule.env` 显式设置 `X_POST_SCHEDULE_MAX_REPAIRS_PER_RUN=17`，与当前 17 账号 schedule 批次容量对齐；legacy daily 值保持不变。

## 数据库变更

新增只增表 `x_post_schedule_drama_scope_compensation_audit` 及 created 索引；不删除、不改写现有表字段。

## 部署步骤

1. 提交并推送 GitHub；记录 40 位 commit。
2. 等待在途 schedule 结束，暂停五个 X timers，在线备份 SQLite/token 哈希/服务状态。
3. 备份 schedule env，写入修复上限 17；从 commit 构建 immutable release，运行迁移和测试，切换 `/opt/x-post-automation/current`。
4. 同步主 API 对应 `service.py`，重启 sidecar 与 API，恢复 timers。
5. 对 run 247 先 validate-only，再以已到期且未占用的当前分钟创建补偿报告，交给自然 scheduler。

## 验证步骤

检查 release commit、服务/timer、DB quick_check/FK、审计唯一行、原 run 不变、补偿 run 终态、17 条发布 URL、future 18:12/22:49 仍保留且范围为 17。

## 回滚方案

切回前一 immutable release，恢复备份的 schedule env 并恢复服务；新增表保留。不得用旧 SQLite/token 覆盖发布后账本。若补偿尚未发布且确认 0 写，可按账本状态单独处置，不删除审计。

## 注意事项

禁止与 scheduler 并发运行 CLI；禁止历史日期补发和真实发布结果不明时重试。
