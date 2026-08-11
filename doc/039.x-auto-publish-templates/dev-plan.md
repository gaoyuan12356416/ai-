# 开发计划

## 开发范围

- 新 X 自动模板后端、页面、独立运行时与文档。
- 现有 X sidecar 的最小增量桥接能力。
- 主 API 代理、导航和权限审计。
- 新旧全套回归与安全部署准备。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 模板/版本/运行/任务/指标存储 | `features/x_auto_posts/core.py` | 已完成 |
| 规则验证、指标源与两层选择 | `features/x_auto_posts/validation.py`、`repositories.py`、`selector.py` | 已完成 |
| X sidecar 安全客户端与执行器 | `features/x_auto_posts/x_sidecar.py`、`publisher.py` | 已完成 |
| loopback API、scheduler/runner/metric | `features/x_auto_posts/service.py`、`scripts/x_auto_post_*.py` | 已完成 |
| 主 API 代理与权限 | `features/x_auto_posts/client.py`、`app.py` | 已完成 |
| 页面与导航 | `static/x-auto-publish-*`、`static/navigation.json`、`static/quick-nav.js` | 已完成 |
| systemd/env/tmpfiles | `deploy/x-auto-post-*`、`.env.example` | 已完成 |
| 测试与文档 | `scripts/test_x_auto_*`、`doc/039.*` | 已完成 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features\x_auto_posts\*.py scripts\x_auto_post_*.py
node --check static\quick-nav.js
node --check static\x-auto-publish-common.js
node --check static\x-auto-publish-templates.js
node --check static\x-auto-publish-template.js
node --check static\x-auto-publish-runs.js
python -m unittest scripts.test_x_auto_post_store scripts.test_x_auto_post_selector scripts.test_x_auto_post_metrics scripts.test_x_auto_post_validation scripts.test_x_auto_post_publisher scripts.test_x_auto_post_runner scripts.test_x_auto_post_x_sidecar scripts.test_x_auto_post_service scripts.test_x_auto_post_deploy scripts.test_x_auto_publish_app_contract scripts.test_x_auto_publish_ui scripts.test_x_post_auto_template_bridge
python -m unittest scripts.test_x_post_priority_manual_store scripts.test_x_post_manual_sidecar scripts.test_x_post_manual_runner scripts.test_x_post_material_pool_selector scripts.test_x_post_daily scripts.test_x_post_schedule_runner scripts.test_x_post_multi_schedule_store scripts.test_x_accounts
python -m unittest scripts.test_x_post_multi_schedule_ui scripts.test_tt_auto_publish_app_contract scripts.test_tt_auto_publish_ui scripts.test_x_accounts_app_contract
git diff --check
```

## 风险与依赖

- 依赖现有 X sidecar 内部 bearer 和账号/队列契约；不得输出其值。
- 依赖只读 MySQL 完整日指标；缓存缺日时失败关闭。
- 生产 live tree 是跨提交复合版本，部署前必须以线上实际 hash 为准构建精确 GitHub release。

## 完成记录

- 2026-08-11：完成独立控制面、既有 X 精确桥接、页面、服务单元和失败关闭状态机；专项、新旧发布及 UI/代理回归通过，未连接真实 X 发帖接口。
