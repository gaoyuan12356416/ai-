# 测试报告

2026-09-24，本地、服务器和公网验收均通过，已上线。

- `python -m unittest discover -s tests -p test_youtube_analytics.py`：40/40通过，无跳过。覆盖归因/多维分组/权限/空值/分页/加权率/金额/CSV/查询约束。
- `python scripts/test_youtube_analytics_deploy.py`：3/3通过，覆盖路由精确追加、导航限制保留、回滚拒绝后续漂移且保留数据库与短链。
- `node --check static/youtube-analytics.js`、`node --check static/quick-nav.js`及QA脚本语法：通过。
- 主服务及技能要求8个Python入口py_compile通过；新后端/部署/验收脚本编译通过；git diff --check通过。
- Playwright隔离mock浏览器：28/28通过，runtime errors=0。覆盖组合筛选/分组/排序/分页/CSV、缺日/全未知/空结果、响应迟到、401/403清屏。桌面1600与手机390宽截图已人工检查，图表/表格/横向滚动正常。
- 截图含明确Mock标记，是合成交互测试，不能作为生产数据证明。

## 浏览器复跑

在工作树启动 `python -m http.server 8907 --bind 127.0.0.1 --directory static`。
然后 `npx --yes --package @playwright/cli playwright-cli -s=youtube-analytics open http://127.0.0.1:8907/youtube-analytics.html`，运行 `npx --yes --package @playwright/cli playwright-cli -s=youtube-analytics run-code --filename tests/youtube_analytics_ui_qa.js`。

截图保存在output/playwright。真实数据、线上HTTP/权限、静态SHA、服务健康及回滚点写入deployment-evidence.md后关闭上线验收。

生产验证已关闭：真实数据守恒、管理员/普通用户HTTP、匿名401、CSV、静态SHA和服务健康全部通过。发现的BUG-001/002/003均修复，详见deployment-evidence.md。
