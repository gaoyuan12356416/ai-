# 开发计划

## 开发范围

新增独立 TT 自动发布模板模块、管理页面、主 API 代理、指标刷新、调度/prepare/publish runner、systemd/nginx 配置、测试和部署文档。旧 TT 发布池业务文件不得变更。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 需求与 SA 设计 | `doc/034.tt-auto-publish-templates` | 已完成 |
| 独立 SQLite 与旧库只读 | `features/tt_auto_posts/core.py`, `legacy_reader.py` | 已完成 |
| 指标仓库与两层筛选 | `repositories.py`, `selector.py` | 已完成 |
| 模板管理与运行页面 | `static/tt-auto-publish-*` | 已完成 |
| API、调度与发布状态机 | `service.py`, `publisher.py`, runners | 已完成 |
| 主 API、导航、部署单元 | `app.py`, `deploy/`, systemd | 已完成 |
| 自动化测试与旧 TT 回归 | `scripts/test_tt_auto_*` + 旧 TT 测试 | 已通过，108 + 64 |
| 浏览器无发布验收与安全复核 | 三个新页面 + 最终安全复核 | 已完成 |
| GitHub-first 部署与关闭默认验收 | CPU 生产服务器 | 待执行 |

## 验证命令

```powershell
python -m unittest scripts.test_tt_auto_post_store scripts.test_tt_auto_post_selector scripts.test_tt_auto_post_metrics scripts.test_tt_auto_publish_ui scripts.test_tt_auto_post_service scripts.test_tt_auto_post_publisher scripts.test_tt_auto_post_links scripts.test_tt_auto_publish_app_contract scripts.test_tt_auto_post_runner -v
python -m unittest scripts.test_tt_post_pool_ui scripts.test_tt_account_settings_ui scripts.test_tt_posts_app_contract -v
node --check static/tt-auto-publish-common.js
node --check static/tt-auto-publish-templates.js
node --check static/tt-auto-publish-template.js
node --check static/tt-auto-publish-runs.js
node --check static/quick-nav.js
git diff --exit-code -- static/tt-post-pool.html static/tt-account-settings.html features/tt_posts
```

## 风险与依赖

- 指标单日聚合仍可能较慢，必须离线按日刷新并对最终 SQL 做生产只读 EXPLAIN。
- 新系统依赖账号快照、旧账号发布设置、旧历史 SQLite、素材 MySQL 和 GPU 回环；任一选择依赖失败应关闭该账号任务。
- 生产发布门禁默认关闭；首次真实 canary 必须另行授权精确账号、素材和时间。

## 完成定义

- 全部新测试与旧 TT 回归通过。
- 三个新页面在真实浏览器完成创建/编辑/复制/启停/手动执行确认和运行详情的无发布验收。
- 最终安全复核无未关闭的高风险问题。
- GitHub 有可回滚提交；生产备份、部署、服务健康、三重发布门禁关闭和无启用模板证据齐全。
