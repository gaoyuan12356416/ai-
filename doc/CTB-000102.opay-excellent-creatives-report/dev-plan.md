# 开发计划

## 开发范围

实现独立 OPay 月度优秀素材静态报表，包括关键词导入、只读日缓存、严格映射与选优、媒体缓存、版本发布、前端、定时任务、测试、文档和生产回填。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 需求与 SA 契约 | Codex | `doc/CTB-000102.*` | 已完成 |
| 关键词工作簿解析 | Codex | `import_keywords.mjs`、版本化 JSON | 已完成 |
| 数据缓存与动态配置 | Codex | Python 生成器、SQLite | 已完成 |
| 选优和审计 | Codex | Python 规则模块 | 已完成 |
| 媒体降级与制作者 | Codex | Python 素材模块 | 已完成 |
| 静态前端与 CSV | Codex | `report.html` | 已完成 |
| 部署单元 | Codex | Nginx/systemd/env | 已完成 |
| 单元/契约/回归 | Codex | `test_*.py`、验证脚本 | 本地通过，生产回归待执行 |
| GitHub 与生产发布 | Codex | 精确 commit/release | 待执行 |

## 编译 / 构建命令

```powershell
python -m py_compile ops\opay-excellent-creatives\opay_excellent_creatives.py
python -m unittest discover -s ops\opay-excellent-creatives -p "test_*.py" -v
python ops\opay-excellent-creatives\validate_frontend_contract.py
git diff --check
```

## 风险与依赖

- 生产只读 MySQL 命令由 `/root/codex_test/opera_product_daily_dashboard.py` 提供，真实凭据不进入仓库。
- FFmpeg 为视频无封面时的可选降级；缺失不阻断月数据。
- Google 精确链路不足时按审计 0 行交付，禁止估算。
- 生产发布前验证数据盘挂载、只读端点、GitHub SSH、Nginx 和旧报表回归。

## 完成记录

- 2026-08-26：独立工作树 `codex/opay-excellent-creatives-report-20260826` 从当前 AI Game Performance 报表分支创建，现有工作树未修改。
- 2026-08-26：使用工作簿运行库只读解析 NG/PK，生成 90 条配置；原 Excel 未改动。
- 2026-08-26：完成生成器、静态页面、Nginx/systemd、23 项自动测试与桌面/390px 浏览器验收。
- 2026-08-26：代码评审修复独立锁、显式关闭继承鉴权及月度维度刷新问题；候选版本可进入生产影子验证。
