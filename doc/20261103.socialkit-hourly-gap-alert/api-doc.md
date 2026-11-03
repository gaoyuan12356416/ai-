# 接口

公开 HTTP API、权限和业务源表不变。

- python -m features.socialkit_analytics.hourly：调度入口，刷新后发送；systemd 加载既有 EnvironmentFile。
- python scripts/socialkit_gap_alert.py --preview --version VERSION：固定版本预览，不发送。
- python scripts/socialkit_gap_alert.py --send --version VERSION：当前 UTC 日发送，独立去重和回执。
- --date 供历史预览，历史禁止生产发送；--socialkit-root、--root 指定数据根。

hourly-status.json 记录小时任务状态；current.json 的 last_full_refresh_beijing_date 记录成功回算。数据根：/mnt/data-disk/socialkit-analytics、/mnt/data-disk/socialkit-gap-alert。
