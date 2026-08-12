# SA 评审意见

## 结论

有条件通过。以下 P0/P1 问题均已纳入需求设计，允许进入开发。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 定时执行 | 仅在 claim SQL 加未来时间会让素材在等待期间被其他流程占用，到期后无法按约执行 | 创建 run 时增加独立 active reservation，并让 pool/queue DB trigger 共同尊重它 | 已采纳 |
| SA-002 | P0 | 时间语义 | `datetime-local` 不带时区，浏览器所在时区可能改变实际执行时间 | UI 明示北京时间并发送 `+08:00`；服务端转 UTC 存储、固定 `Asia/Shanghai` 展示 | 已采纳 |
| SA-003 | P0 | runner | scheduled queued row 若仍被原 claim 选中，会提前预检和发布 | claim 仅选择 immediate、已到期 scheduled 或已 running 的 run | 已采纳 |
| SA-004 | P0 | 幂等 | 定时时间已过后，响应丢失重试若先做“未来时间”校验会无法读回原任务 | 先按规范时间核对已有幂等 run；只有新建时才要求未来 | 已采纳 |
| SA-005 | P1 | schema | 重建带 CHECK 的旧表会破坏生产账本和回滚边界 | 只 ADD COLUMN/CREATE TABLE/INDEX/TRIGGER；旧行默认 immediate | 已采纳 |
| SA-006 | P1 | 失败释放 | 定时预检失败若永久占用素材，会把零写入失败误当成发布历史 | 无 queue 的 failed_preflight 将 reservation 置 released，保留审计 | 已采纳 |
| SA-007 | P1 | 自动模板 | `x_post_manual_run` 同时承载 auto_template，新增字段可能改变自动任务 | auto_template 固定 immediate；测试其创建、claim、恢复与手动 reservation 冲突 | 已采纳 |
| SA-008 | P1 | UI | 页面每 2.5 秒轮询数天的 future run 会制造无意义流量 | 远离到期时降为 30 秒，临近/到期后恢复 2.5 秒 | 已采纳 |
| SA-009 | P1 | 部署 | 开发期间生产先切到 `46e0720…`，随后又在部署闸门前切到 `09d267db…`；继续使用旧基线会回退 Premium relay/repost 或 operator-manual 素材复用 | 每次以实时 live commit 重放本功能，完整回归后构建精确 release、逐文件备份并窄重启 | 已采纳 |
| SA-010 | P2 | 范围 | 取消/改期会引入 reservation 释放与竞态语义 | 本期不实现，提交前二次确认 | 已采纳 |
| SA-011 | P0 | 素材复用 | 初版 reservation 拒绝 pool/历史 queue，与新 live 的 operator-manual 显式复用冲突；直接部署会造成行为回退 | manual parent 允许在既有 pool/历史 queue 上建 reservation；active 后排除自动候选和其他任务，auto_template 仍保持全局去重 | 已采纳 |

## 决策记录

- run 仍使用现有 `queued/running/...` 状态集合；“等待定时”由 `queued + scheduled_at>now` 派生，避免扩张状态机。
- scheduled run 的 `run_date` 取定时时间对应的北京时间日期，`source_date` 为前一日。
- 预检在到期后执行，不提前刷新 token 或下载媒体；等待期间只做轻量持久化与去重占用。
- reservation 是从任务创建时开始的并发占用，不是历史去重证据；operator manual 可复用提交前已有的 pool/queue，旧队列和 pool 行保持不变。
- 不增加新 timer，复用 15 秒 `x-post-manual.timer` 和共享发布锁。

## PM 修订确认

2026-08-12：已把 SA-001 至 SA-011 全部写入 `requirements.md`，无遗留阻断项。
