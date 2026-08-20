# 测试报告

## 测试结论

本地完整回归、浏览器 QA 和生产只读验收全部通过；列表优先页面已完成受控静态发布。

## 测试范围

FB 模板列表/表单静态页、公共前端脚本、既有 API 契约。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| JavaScript 语法 | 3 | 3 | 0 | 0 |
| FB 全量自动化 | 80 | 80 | 0 | 0 |
| X/TT 主契约回归 | 66 | 66 | 0 | 0 |
| 浏览器场景 | 11 | 11 | 0 | 0 |
| 生产发布前快照 | 1 | 1 | 0 | 0 |
| 生产发布后验收 | 1 | 1 | 0 | 0 |

## 缺陷情况

第一轮代码评审发现确认框监听清理问题，已在完整 QA 前修复，未登记为 QA 缺陷。

## 验证证据

- `node --check static/fb-auto-publish-{common,templates,template}.js`：通过。
- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：Ran 80 tests，OK。
- `scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract scripts.test_tt_posts_app_contract`：Ran 66 tests，OK。
- Playwright：列表首屏无表单；创建/编辑跳转与详情回填通过；创建/更新请求分别正确省略/携带 `expected_version=3`；筛选发出 `q=英语&status=disabled`；第二页发出 `offset=50`；启用请求携带版本；run-now 携带 UUID `operation_id`。
- 390×844：`documentElement.scrollWidth=375 <= innerWidth=390`，列表标题及“创建模板”在首屏可见；控制台 0 error/0 warning。
- 生产发布前：`live_enabled=false`，sidecar `NRestarts=0`，六张业务表均为 0，`PRAGMA quick_check=ok`。
- GitHub-first：发布提交 `bda9e7f347d8cd81743f26b65ee4f3e128504e4e`；服务器不可变 release 的 HEAD 与远端分支一致，服务器端前端契约 5/5、三个 `node --check` 通过。
- 公网验收：六个静态 URL 均为 HTTP 200；release、应用目录、Nginx 目录与公网响应的各文件 SHA-256 全部一致；列表入口无 `templateForm` 且指向独立创建页，创建页含 `templateForm` 和返回列表链接。
- 浏览器只读验收：Chrome 与应用内浏览器均渲染新页面标题/说明；匿名会话正确显示登录门禁，控制台 0 error/0 warning。生产会话无登录态，未伪造 Cookie；登录态完整交互由同提交的本地 mock API 场景覆盖。
- 生产发布后：`live_enabled=false`；sidecar `MainPID=3083645`、`NRestarts=0`、`active/running`；template/run/task/due-slot/attempt/ledger 均为 0；`PRAGMA quick_check=ok`。
- 全程未创建或修改生产模板，未调用 enable/disable/run-now，未产生 Graph Post，未重启服务或 reload Nginx。

## 遗留风险

- 当前浏览器没有生产登录态，因此未在生产会话调用模板读取 API；该 API 与后端未变，登录态页面交互已由契约测试和本地浏览器 mock 覆盖。静态文件公网哈希、匿名门禁和生产运行状态已实测。

## 发布建议

发布验收通过；继续保持 `FB_AUTO_POST_LIVE_ENABLED=false`，后续正常运营使用前再按独立流程开放真实 Graph 发布。
