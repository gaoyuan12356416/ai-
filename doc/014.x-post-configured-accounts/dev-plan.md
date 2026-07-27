# 014.x-post-configured-accounts 开发计划

## 开发范围

把 daily 账号数从固定 3 改为配置驱动的 N（1 至 50），在账号列表增加只读派生状态，并补齐配置、同日冻结、全批预检和原有安全策略的回归。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 通用账号配置解析与 1..50 边界 | 开发 | `scripts/x_post_daily_runner.py`、`features/x_accounts/oauth_service.py` | 已完成 |
| 动态 N 账号/候选/计划/响应校验 | 开发 | runner、sidecar、`features/x_posts/service.py` | 已完成 |
| 同日旧计划冻结及禁止扩容补发 | 开发 | runner、daily plan query | 已完成 |
| 逐账号配置状态 DTO | 开发 | `features/x_accounts/oauth_service.py`、client/app 契约 | 已完成 |
| 管理员列表状态列 | 开发 | `static/x-account-list.html` | 已完成 |
| env 示例与生产双文件一致性门禁 | 开发/运维 | `deploy/x-post-*.env.example`、生产 env | 代码完成/生产待执行 |
| 单元、契约、回归和零补发验证 | QA | `scripts/test_x_*.py` | 离线 197 项通过 |
| GitHub-first 部署与自然批次观察 | 开发/运维 | CPU release、systemd timer | 待执行 |

## 实施顺序

1. 先补充配置解析、DTO、动态 N 和同日冻结测试。
2. 修改 sidecar 的 scope、plan、query、publish 校验，删除固定三账号假设。
3. 修改 runner 的候选数量、预检、计划响应、发布汇总和恢复逻辑。
4. 修改账号列表 UI 和页面契约。
5. 执行全套 X 离线测试；不在测试中调用真实 X。
6. 推送精确 GitHub commit，备份后更新两份 env 和 immutable release。
7. 部署当天只验证状态与计数，不手工运行 daily；等待下一自然 timer。

## 编译 / 构建命令

```powershell
python -m py_compile features\x_accounts\oauth_service.py features\x_posts\service.py scripts\x_post_daily_runner.py
python scripts\test_x_accounts.py
python scripts\test_x_accounts_app_contract.py
python scripts\test_x_post_daily.py
python scripts\test_x_posts.py
python scripts\test_x_post_ledger.py
git diff --check
```

管理员页面行为由 `test_x_accounts_app_contract.py` 和生产浏览器验收覆盖；测试报告需记录实际执行命令及结果。

## 风险与依赖

- 依赖 9 个账号均为可发布状态，且素材池有至少 9 条互异可用素材。
- 依赖 sidecar 与 daily service 使用同一有序账号 ID 配置。
- 批次增大可能增加下载、GPU 修复和顺序发布时间，需要覆盖 50 个配置解析和 9 个生产执行窗口。
- 本次生产配置同时设置 `X_POST_DAILY_MAX_REPAIRS_PER_RUN=9`，并把 oneshot 超时提高至 360 分钟。
- 任何真实 Post 都只能来自后续自然 timer；本次部署验证不得手工启动 daily service。

## 完成记录

待代码实现、评审、测试和生产验证完成后补充精确 commit、测试数量、备份路径、release、部署前后计数及下一触发时间。
