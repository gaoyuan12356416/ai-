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
| 单测、浏览器和故障注入 | Codex/QA | `tests/`, Playwright | 已完成 |
| GitHub-first 生产部署 | Codex | CPU 服务器 | 已完成 |

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

- 生产源 commit：
  `bfe4bc499b95470dba55ff158015b7f5b5ea113c`。
- Python 43/43、Node 53/53、验收用例 29/29。
- 生产 release：
  `/mnt/data-disk/tt-drama-featured/releases/ai-tt-featured-bfe4bc499b95470d`。
- 生产 backup：
  `/mnt/data-disk/tt-drama-featured/backups/20260727T151000+0800-bfe4bc4`。
- 快照 hash：
  `37e3a126a258e03b89ec743f08300e9d5582dc07f92916349b45c7dec2f5b2df`。
- timer enabled/active；15:30 首次自动触发成功，下一次 18:00；
  主 API 未重启，`NRestarts=0`。
