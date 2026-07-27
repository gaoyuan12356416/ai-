# 开发计划

## 开发范围

新增只读离线排行、数据盘 LKG、本地 JSON 路由与 `/tt` 动态可点击卡片；
不改广告状态、搜索 resolver 或数据库结构。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 生产字段/索引/日期/回填核验 | Codex | 63350 只读 SQL | 已完成 |
| 需求与 SA 评审 | PM/SA | `doc/016.tt-featured-top-spend/` | 已完成 |
| 测试用例与 SA 评审 | QA/SA | `test-cases.md`, `sa-test-review.md` | 已完成 |
| 排行、校验与原子快照 | Codex | `features/tt_drama_featured/`, `scripts/` | 已完成 |
| 本地 JSON 路由与定时器 | Codex | `deploy/` | 已完成 |
| 动态卡片与点击透传 | Codex | `static/tt-drama-search.*` | 已完成 |
| 单测、浏览器和故障注入 | Codex/QA | `tests/`, Playwright | 进行中 |
| GitHub-first 生产部署 | Codex | CPU 服务器 | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile features\tt_drama_featured\service.py scripts\refresh_tt_drama_featured.py
python -m unittest tests.test_tt_drama_featured_service tests.test_tt_drama_resolver_service tests.test_tt_drama_resolver_http tests.test_tt_drama_resolver_app_contract
node --check static\tt-drama-search.js
node scripts\test_tt_drama_bridge.js
git diff --check
```

## 风险与依赖

- 依赖 CPU 服务器现有 PyMySQL 和 `.env` 中只读数据库配置。
- 依赖 `/mnt/data-disk` UUID、空间、Nginx 遍历权限和 systemd timer。
- 生产昨日数据延迟回填，必须保留 18:00 对账。
- `output/` 是既存未跟踪浏览器产物，禁止加入提交。

## 完成记录

待实现、验证和部署后补充 commit、测试数、快照与 timer 证据。
