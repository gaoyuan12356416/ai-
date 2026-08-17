# 开发计划

## 开发范围

普通 material schedule runner、Sidecar OAuth 信任边界、XPostStore schema guard/plan/repost 状态机、专项与回归测试、交付文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 稳定随机同语言配对 | Backend | `scripts/x_post_schedule_runner.py` | 完成 |
| relay 当前资格复核 | Backend | `features/x_accounts/oauth_service.py` | 完成 |
| 原子计划、trigger、repost pool 状态 | Backend | `features/x_posts/service.py` | 完成 |
| 专项/回归用例 | QA | `scripts/test_x_post_material_random_relay.py` 等 | 完成 |
| 需求流与部署回滚 | PM/SA | `doc/047...` | 完成 |

## 编译 / 构建命令

```powershell
python -m py_compile features/x_posts/service.py features/x_accounts/oauth_service.py scripts/x_post_schedule_runner.py scripts/test_x_post_material_random_relay.py
python -m unittest scripts.test_x_post_material_random_relay
python -m unittest discover -s scripts -p 'test_x_*.py'
git diff --check
```

## 风险与依赖

- 依赖 Sidecar premium relay account API 提供当前 Token entitlement 与 canonical drama language。
- 不执行真实 X 写入；生产验证只能使用 health、mock、自然 timer 和 ledger invariants。

## 完成记录

- 2026-08-17：代码与专项完成；最终全量命令结果见 test-report.md。
