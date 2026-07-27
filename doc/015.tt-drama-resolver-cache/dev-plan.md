# 开发计划

## 开发范围

- 新增 resolver 服务模块和自动化测试。
- 在现有 AI API 中增加精确公共 GET 路由。
- 把 `/tt` 从“格式通过即跳转”改为“远端确认后展示卡片并跳转”。
- 增加性能观测、CSP/CDN 白名单、部署与回滚文档。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与 SA 评审 | PM/SA | `doc/015.tt-drama-resolver-cache/` | 已完成 |
| Resolver 查询与缓存 | Codex | `features/tt_drama_resolver/` | 已完成 |
| 公共 HTTP 路由 | Codex | `app.py` | 已完成 |
| 动态结果卡与失败关闭 | Codex | `static/tt-drama-search.*` | 已完成 |
| CSP 与配置 | Codex | `deploy/nginx/tt-drama-search.conf`, `.env.example` | 已完成 |
| 自动测试和浏览器回归 | QA/Codex | `tests/`, `scripts/test_tt_drama_bridge.js` | 本地已完成，生产待验 |
| GitHub、部署、测速与回滚证据 | Codex | GitHub / CPU server / 文档 | 待开始 |

## 编译 / 构建命令

```powershell
python -m py_compile app.py features\tt_drama_resolver\service.py
python -m unittest tests.test_tt_drama_resolver_service tests.test_tt_drama_resolver_http tests.test_tt_drama_resolver_app_contract
node --check static\tt-drama-search.js
node scripts\test_tt_drama_bridge.js
git diff --check
```

## 风险与依赖

- 线上必须安装 PyMySQL，并且 `DRAMA_DB_*` 指向 `@@read_only=1` 的 63350。
- 首次查询受数据库连接和索引页状态影响；部署后必须分别测冷/热/封面。
- 当前服务是线程化 HTTPServer，缓存和 single-flight 必须线程安全。
- `/tt` 同时由服务静态目录和 Nginx 发布目录提供，部署时只同步明确文件。

## 完成记录

- 2026-07-27：完成需求、SA 风险审查和本地/线上基线核对。
- 2026-07-27：完成 resolver、公开路由、精确 Nginx 代理、动态结果卡和本地移动端浏览器验证。
- 2026-07-27：确认线上 `content_id` 为 `utf8mb4_unicode_ci`，实现 SQL 二进制精确匹配和 canonical ID 二次校验。
