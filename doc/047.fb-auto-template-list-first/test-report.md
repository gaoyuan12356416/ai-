# 测试报告

## 测试结论

本地完整回归和浏览器 QA 通过，可进入受控静态发布；生产只读验收尚待执行。

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
| 生产发布后验收 | 待执行 | 0 | 0 | 待执行 |

## 缺陷情况

第一轮代码评审发现确认框监听清理问题，已在完整 QA 前修复，未登记为 QA 缺陷。

## 验证证据

- `node --check static/fb-auto-publish-{common,templates,template}.js`：通过。
- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：Ran 80 tests，OK。
- `scripts.test_x_accounts_app_contract scripts.test_tt_auto_publish_app_contract scripts.test_x_auto_publish_app_contract scripts.test_tt_posts_app_contract`：Ran 66 tests，OK。
- Playwright：列表首屏无表单；创建/编辑跳转与详情回填通过；创建/更新请求分别正确省略/携带 `expected_version=3`；筛选发出 `q=英语&status=disabled`；第二页发出 `offset=50`；启用请求携带版本；run-now 携带 UUID `operation_id`。
- 390×844：`documentElement.scrollWidth=375 <= innerWidth=390`，列表标题及“创建模板”在首屏可见；控制台 0 error/0 warning。
- 生产发布前：`live_enabled=false`，sidecar `NRestarts=0`，六张业务表均为 0，`PRAGMA quick_check=ok`。

## 遗留风险

- 尚未完成生产静态发布后的公网哈希、已登录只读页面和账本不变量复核。

## 发布建议

允许按 `deploy.md` 执行仅静态文件发布；禁止创建生产模板、调用 run-now 或开启真实 Graph 发布。
