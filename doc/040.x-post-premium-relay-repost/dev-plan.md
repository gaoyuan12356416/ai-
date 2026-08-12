# 开发计划

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 数据库与状态机 | `features/x_posts/service.py` | 完成 |
| 会员识别、发布/转发编排 | `features/x_accounts/oauth_service.py` | 完成 |
| 短剧预检与会员中转路由 | `scripts/x_post_schedule_runner.py` | 完成 |
| 专项、现有回归 | `scripts/test_x_*.py` | 完成 |
| 文档、部署/回滚 | `doc/040...` | 完成 |

## 验证命令

```powershell
python -m py_compile features\x_posts\service.py features\x_accounts\oauth_service.py scripts\x_post_schedule_runner.py
python -m unittest scripts.test_x_post_premium_relay_repost
$tests = Get-ChildItem scripts -Filter 'test_x_*.py' | % { 'scripts.' + $_.BaseName }; python -m unittest $tests
git diff --check
```

## 完成记录

- 从生产提交 `0e03210cd2c5c80b134884f9e96304797efa2545` 建独立分支。
- 未读取或写入生产 token，未触发生产发布。
