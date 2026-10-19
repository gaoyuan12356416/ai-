# 开发计划

1. 独立分支 codex/screenshot-mobo-products-20261019，基于已发布截图修复分支；读取生产快照作局部部署基线。
2. features/screenshot_products.py 集中定义四产品、三自动 profile 和新产品封面查询；app.py 接入两个函数和专用 GET。
3. runner 增加 --all-products/--profile，按产品隔离，保留原单产品 CLI；日期显式按 UTC+8 昨天冻结。cron 仍使用现有锁与时间。
4. QA 用 unittest 和 AST 提取避免导入 app.py 服务副作用；运行 Python 编译、内联 JS 语法检查、git diff --check。
5. 推送 GitHub 后由服务器拉取精确提交，预检/备份/窄重启、API/原图和素材库验收，再启用多产品 wrapper 并执行昨日首轮。
