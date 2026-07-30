# 012.x-post-material-pool 开发计划

## 开发范围

在现有 X 每日发布链路上增加 AI 后台人工素材池，并将正式选材从前日 spend 排名改为全局池 FIFO；保留三个固定账号、发布数据/媒体门禁、W2A/短链、违规与内容标签审计、日志和发布失败语义。

## 任务拆分

| 任务 | 文件/模块 | 当前状态 |
| --- | --- | --- |
| 需求、SA、QA、API、部署文档 | `doc/012.x-post-material-pool/` | 本次完成 |
| 素材池表、迁移、触发器、事务与派生查询 | `features/x_posts/service.py` | 已实现并回归 |
| 人工池 FIFO、Dramawave/剧映射校验与非阻塞合规审计 | `features/x_posts/selector.py` | 已实现并回归 |
| daily 池读取、检查回写、媒体补位和成组计划 | `scripts/x_post_daily_runner.py` | 已实现 |
| Sidecar 管理/daily 路由及安全 DTO | `features/x_accounts/` | 已实现 |
| 主后台管理员 API、审计和 no-store | `app.py` | 已实现 |
| 素材池页面、导航、日志页文案 | `static/` | 已实现 |
| 素材池、selector、runner、ledger、API/DOM 回归 | `scripts/test_x_*.py` | 当前工作树 362/362 通过，1 项 Windows 软链接权限用例按环境跳过 |
| GitHub-first 合并与生产部署 | Git / 43.166.187.96 | 未执行 |

## 编译 / 构建命令

```powershell
python -m py_compile features/x_accounts/oauth_service.py features/x_accounts/client.py features/x_posts/__init__.py features/x_posts/service.py features/x_posts/selector.py scripts/x_post_daily_runner.py
python -m unittest -v scripts.test_x_post_material_pool scripts.test_x_post_material_pool_selector scripts.test_x_post_daily scripts.test_x_post_ledger scripts.test_x_accounts_app_contract
python -m unittest -v scripts.test_x_posts scripts.test_x_accounts scripts.test_x_account_owner_backfill
node --check static/quick-nav.js
git diff --check
```

## 实施顺序

1. 固化需求和状态/排重合同。
2. 增量迁移 SQLite，先建立全局唯一键和双向跨表约束。
3. 实现池管理 service 与内部/管理员 API。
4. 实现不依赖 insight 的 Dramawave FIFO selector。
5. 将 runner 切到素材池并保持三条成组预检。
6. 实现管理页面、导航和日志页口径。
7. 执行专项与全部 X 离线回归，独立 SA 代码复审。
8. 仅在 P1/P2 关闭、生产副本迁移通过后走 GitHub-first 部署。

## 风险与依赖

- 依赖 `ads_custom_source.product`、素材/剧映射、四类违规表和 `resource_tags` 的生产字段与只读权限。
- 依赖素材 HTTPS 地址、数据盘、ffprobe、Sidecar、三个 X 账号、W2A 和短链服务。
- SQLite 触发器是 legacy 表外键/跨表排重的重要防线，不能只靠 Python 预检查。
- 原始池扫描窗口为最老 1000 条，可发布候选媒体补位窗口为 50 条；超过 1000 的长期数据错误前缀需要管理员处理。
- 生产主后台为 composite，部署必须核对 live 基线并保留并行功能。

## 完成记录

- 2026-07-23：文档阶段未修改生产、未部署、未调用真实 X。
- 最终关键修订后，本地离线专项与全部 X 回归共 139/139 通过；包含 205 条检查结果按 100/100/5 分批回写，详细分组见 `test-report.md`。
