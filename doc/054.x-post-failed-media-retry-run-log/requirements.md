# X 失败媒体一次性重试与运行批次日志修复

## 背景

- `2026-08-24` 的素材定时批次 318、320 已完成，但存在发布前媒体失败。
- 失败代码仅为 `invalid_media_codec`、`invalid_media_dimensions`、`media_too_large`。
- 后台“每日运行批次”只读取已停用的 `x_post_daily_run`，因此最近日期停留在 `2026-07-27`；实际运行由 schedule/claim timer 驱动并写入 `x_post_schedule_run`。

## 范围

1. 只恢复指定同日素材批次的完整失败集合。
2. 每个素材先重制、重新下载并通过媒体预检，再替换冻结队列中的媒体 URL 与指纹。
3. 保留账号、素材、文案、目标账号和 Premium relay 映射；不重新选材。
4. 只允许 `attempt_count=0`、`unknown_outcome=0`、无 Post/Repost ID 或 URL 的队列进入恢复。
5. 每个 queue 只允许恢复一次，并写入独立审计表。
6. 运行批次接口合并当前 `x_post_schedule_run` 与历史 `x_post_daily_run`，页面明确区分“定时批次”和“日批次”。

## 非范围

- 不启用已废弃的 `x-post-daily.timer`。
- 不替换账号、素材、语言、文案或 relay 计划。
- 不自动重试任何结果未知或已经触发 X 写入的记录。
- 不删除原失败日志、原素材 URL 或历史批次。

## 验收

- API 最近运行首条为当天 schedule 批次。
- 原 timers 恢复，daily timer 仍为 masked/inactive。
- 恢复前 validate-only 对完整目标集合通过，数据库不发生变化。
- apply 后审计行数等于目标数，队列进入 queued/reserved，批次进入 running。
- 执行一次 schedule worker 后逐条对账 Post/Repost 结果；未知结果为 0。
- SQLite `quick_check=ok` 且 `foreign_key_check=0`。
