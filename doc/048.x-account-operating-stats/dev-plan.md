# 开发计划

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 统计、归属、缓存合并 | `features/x_account_stats/service.py` | 完成 |
| twice-daily refresh | refresh script、systemd unit/timer | 完成 |
| 管理 API | `app.py`、`.env.example` | 完成 |
| 六项 UI/未归属/公众指标 | `static/x-account-list.html` | 完成 |
| focused/合同回归 | 新增及既有 X tests | 完成 |

## 验证命令

```powershell
python -m unittest scripts.test_x_account_operating_stats scripts.test_x_accounts_app_contract scripts.test_x_membership_duration_ui -v
python -m unittest scripts.test_x_accounts -q
python -m unittest scripts.test_x_post_ledger scripts.test_x_post_material_random_relay -q
python -m py_compile features\x_account_stats\service.py scripts\refresh_x_account_operating_stats.py app.py
node --check static\quick-nav.js
git diff --check
```

生产依赖：现有 `/usr/bin/mysql -> /usr/local/bin/mysql-gated` 和只读 63350 配置；部署不在本任务授权内。
