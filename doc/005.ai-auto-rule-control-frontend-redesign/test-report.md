# 测试报告

## 测试结论
正式前端实现静态检查和浏览器 mock 验收通过。`ad-control-rules.html` 已改为规则组列表 + 抽屉式创建/编辑；产品枚举仅保留 `dramawave`、`hotdrama`、`freereels`；账号在选择产品后按产品加载并分组展示。

## 测试范围
静态前端实现、公共导航配置、规则组列表聚合、抽屉创建流程、产品/账号选择口径。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 静态检查 | 4 | 4 | 0 | 0 |
| 浏览器 mock 验收 | 7 | 7 | 0 | 0 |

## 缺陷情况
暂无。

## 验证证据
- `python -m py_compile app.py` 通过。
- `node --check static/quick-nav.js` 通过。
- `node --check static/ad-control-pages.js` 通过。
- `git diff --check` 通过。
- Playwright 使用本机 Chrome 打开本地 `ad-control-rules.html`，mock `/api/ui/topbar`、`/api/ad-control/bindings`、`/api/ad-control/accounts`。
- 抽屉产品枚举为 `dramawave`、`hotdrama`、`freereels`，未出现 `3348`、`bestreels`、`PrestaGo`、`LoadCash` 等无关产品。
- 选择 `dramawave + hotdrama` 后，账号列表只展示这两个产品下的账号。
- 规则组列表能把 `dramawave/hotdrama` 两条底层 binding 聚合为一个前端规则组。
- 保存流程 mock 验证通过：`dramawave + hotdrama` 会生成 2 条 `rule_set`、2 条 `account_group`、2 条 `binding`；binding 均为 `enabled=false`，并使用同一个 `strategy.frontend_rule_group_id` 聚合。
- 手动添加账号 mock 验证通过：当 `/api/ad-control/accounts` 未返回目标账号时，在抽屉粘贴 account_id 仍能加入已选账号，并保存到 3 个产品的 `account_group` 与 `binding.strategy.selected_account_ids`；binding 仍为 `enabled=false`。
- 截图：`implemented-rules-drawer.png`。
- 截图：`manual-account-add.png`。
- Playwright 打开 `prototype/ad-control-redesign-preview.html` 成功。
- 截图：`prototype/ad-control-redesign-preview.png`。
- 页面标题：`AI自动规则调控 - 前端重设计原型`。
- 渲染尺寸：`1600 x 1757`。
- Playwright 打开 `usage-guide.html` 成功。
- 使用说明截图：`usage-guide.png`。
- 使用说明包含 14 个章节、14 个目录项，覆盖产品枚举、账号加载、规则组保存映射、Preview/执行、日志和检查表。

## 遗留风险
- 浏览器验收使用 mock API；上线前仍需用线上登录态访问真实页面确认接口权限和真实账号列表耗时。
- 本次只重写前端交互，不启用自动 runner，不做真实广告关闭验证。

## 发布建议
可进入 GitHub-first 提交与部署流程；部署后先只做页面访问和只读 preview，不做真实关闭。
