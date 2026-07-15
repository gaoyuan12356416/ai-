# 开发计划

## 开发范围

新规则组 live pause 的批次选择、Graph 执行、runner 续跑、`ads_ai` 审计存储、日志 API/UI、历史回填与生产窄补丁部署。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 公平批次与错误分类 | Codex | `features/ad_control_execution_log/service.py` | 完成 |
| live execute 并发/熔断/日志集成 | Codex | `deploy/apply_ad_control_execution_log_fix.py` | 完成 |
| runner 续跑状态机 | Codex | `scripts/ad_control_rule_runner.py` | 完成 |
| ads_ai DDL 与回填 | Codex | SQL、`migrate_ad_control_action_logs.py` | 完成 |
| 日志 UI 与缓存版本 | Codex | `static/ad-control-*` | 完成 |
| 自动化测试与生产同源演练 | Codex | `tests/`、临时复合 app | 完成 |
| GitHub-first 上线与在线回归 | Codex | server 43.166.187.96 | 已部署；登录UI待验收 |
| 业务日分组与状态 reducer | Codex | `features/ad_control_execution_log/service.py` | 完成 |
| daily/raw API 与生产窄补丁 | Codex | `deploy/apply_ad_control_execution_log_fix.py` | 完成 |
| 日卡片、批次清单与状态文案 | Codex | `static/ad-control-pages.js|css`、HTML cache buster | 完成 |
| 日聚合/分页/跨午夜/历史状态回归 | Codex | `tests/` | 完成 |

## 编译 / 构建命令

```powershell
python -m py_compile scripts/ad_control_rule_runner.py deploy/apply_ad_control_execution_log_fix.py features/ad_control_execution_log/service.py scripts/migrate_ad_control_action_logs.py
node --check static/ad-control-pages.js
python -m unittest discover -s tests -p 'test_*.py' -v
python deploy/apply_ad_control_execution_log_fix.py --root <production-snapshot> --check
```

## 风险与依赖

- 依赖生产已有 MySQL 配置和 PyMySQL；已验证账号对 `ads_ai` 有 CREATE/INSERT/UPDATE/DELETE 权限。
- 依赖 Meta token 与 Graph API；上线验证先做 preview/dry-run，不以真实暂停作为首次验证。
- 生产 app 是共享复合版，只允许窄补丁，禁止用仓库 app.py 覆盖。
- runner cron 每 5 分钟；部署需避免与正在运行的 runner 冲突，并只重启 `drama-material-api.service`。

## 完成记录

- 2026-07-15：本地实现、21 项单元/契约测试和生产同源补丁幂等演练通过。
- 2026-07-15：现网只读审计确认7月15日最终完成；7月14日末批为Meta限流错误，历史迁移误显示为partial。进入日汇总修复。
- 2026-07-15：业务日读模型、按日保守状态归并、daily/raw接口、日卡与批次懒加载完成；59项测试和当前生产同源app补丁幂等编译通过。
- 2026-07-15：功能提交 `d4af68af83e55b4df65fc13f273738ba98dfe189` 已推送并部署；本地/服务器 checkout 59/59，compile、JS和diff检查通过。生产只读数据确认7月15日完成、7月14日限流后未完成；未触碰runner、Meta或DB数据。
- 2026-07-15：服务重启后 Chrome/IAB 登录态失效，真实登录 UI 视觉验收仍待重新登录，不作为已完成项。
