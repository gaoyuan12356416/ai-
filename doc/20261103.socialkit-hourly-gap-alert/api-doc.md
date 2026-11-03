# 接口

公开 HTTP API、权限和业务源表不变。

- python -m features.socialkit_analytics.hourly：调度入口，刷新后发送；systemd 加载既有 EnvironmentFile。
- python scripts/socialkit_gap_alert.py --preview --version VERSION：固定版本预览，不发送。
- python scripts/socialkit_gap_alert.py --send --version VERSION：当前 UTC 日发送，独立去重和回执。
- --date 为播报 UTC 日期，收入自动取之前最近 7 个完整 UTC 日；供历史预览，历史禁止生产发送。--socialkit-root、--root 指定数据根。

comparison.json 增加 `period={start,end,days:7,timezone:UTC}`、`rule_version=seven-complete-utc-days-v1`、`rules` 和 `own_source_updated_by_date`。事件保留播报 `date` 作为原去重键，增加同一 `period`、`reasons` 和 `revenue_gap_cents`；`own_revenue_cents` 为实际区间金额，不再固定为零。`state` 为首个命中原因：`no_confirmed_publication` 或 `large_revenue_gap`，同时命中时 `reasons` 包含两者。summary/status 同步记录区间和规则版本。历史已发送卡片保持冻结，不改写。

hourly-status.json 记录小时任务状态；current.json 的 last_full_refresh_beijing_date 记录成功回算。数据根：/mnt/data-disk/socialkit-analytics、/mnt/data-disk/socialkit-gap-alert。
