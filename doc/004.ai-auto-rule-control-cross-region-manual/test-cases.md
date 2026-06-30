# 测试用例

## 测试范围
本次覆盖 AI 自动规则调控跨区手动配置优化：
- 七个页面与公共顶吸/快速导航。
- `+8` 账户池、国家组、跨区规则模板、绑定策略字段。
- 后端 `account_time_zone`、`country` 条件匹配。
- 线上只读验证不产生广告动作。

## 测试数据
- 本地仓库：`D:\codex\ai-drama-material-service-ad-control-deploy`
- 分支：`codex/ai-auto-rule-control`
- 线上服务：`https://ai.yingliangads.com`
- 线上作业状态库：`/root/drama_material_service/data/drama_material_jobs.sqlite3`
- 线上发布 commit：`bcde108bef92fb733af03a8eebeb67d05dfd9571`

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | Python 编译 | 本地代码已拉取 | 执行 `python -m py_compile app.py` | 无语法错误 | P0 | 通过 |
| TC-002 | 公共 JS 语法 | 本地代码已拉取 | 执行 `node --check static/quick-nav.js` | 无语法错误 | P0 | 通过 |
| TC-003 | 调控页面 JS 语法 | 本地代码已拉取 | 执行 `node --check static/ad-control-pages.js` | 无语法错误 | P0 | 通过 |
| TC-004 | 静态资源引用 | 本地代码已拉取 | 检查 7 个页面引用 `ui-topbar.css`、`ui-topbar.js`、`quick-nav.js`、`ad-control-pages.js` | 所有页面均引用公共资源 | P0 | 通过 |
| TC-005 | 导航配置完整性 | 本地 `navigation.json` | 检查 `ad_control` 分组和 `ad_material_test` 分组 | `ad_control` 有 7 个子入口，旧分组不丢失 | P0 | 通过 |
| TC-006 | 绑定策略字段 | 本地临时 SQLite | 保存 binding 并读取 | `strategy_json` 可保存关闭时间、执行时区、同日禁止重启等字段 | P0 | 通过 |
| TC-007 | 新绑定默认关闭 | 本地临时 SQLite | 创建新 binding | `enabled=0` | P0 | 通过 |
| TC-008 | 时区和国家组正向匹配 | 本地规则匹配 | item 为 `account_time_zone=UTC+08:00`、`country=ww-4` | 命中 `pause` | P0 | 通过 |
| TC-009 | 国家组负向匹配 | 本地规则匹配 | item 为 `country=US` | 不命中关闭 | P0 | 通过 |
| TC-010 | 时区负向匹配 | 本地规则匹配 | item 为 `account_time_zone=+7` | 不命中关闭 | P0 | 通过 |
| TC-011 | 白名单字段补充 | monkeypatch 业务库查询 | 检查 whitelist SQL 和返回值 | SQL 查询 `d.country`、`s.time_zone`，返回 `country`、`account_time_zone` | P0 | 通过 |
| TC-012 | 公网页面状态 | 线上 HTTP | 请求 7 个页面和公共资源 | 页面/资源均返回 200 | P0 | 通过 |
| TC-013 | 受保护 API | 未登录态 | 请求 `/api/ad-control/runner/status` | 返回 401 | P1 | 通过 |
| TC-014 | 公网 JS 内容 | 线上静态资源 | UTF-8 读取 `ad-control-pages.js` | 包含 `+8账户池批量创建`、`+8跨区关停模板`、`跨区调控绑定向导`、`account_time_zone`、`created_data.country` | P0 | 通过 |
| TC-015 | 浏览器 DOM | Playwright Chrome | 打开 7 个页面 | 页面 200，`UiTopbar` 和 `QuickNav` 对象存在 | P0 | 通过 |
| TC-016 | 快速导航渲染 | Playwright Chrome | 模拟管理员权限渲染 QuickNav | `AI自动规则调控` 分组 7 个子入口，能渲染链接并高亮规则集 | P0 | 通过 |
| TC-017 | 线上服务状态 | SSH 线上 | `systemctl is-active drama-material-api.service` | 返回 `active` | P0 | 通过 |
| TC-018 | 线上审计计数 | 线上状态库 | 查询 `ad_control_action` | 数量为 0，部署/测试未产生广告动作审计 | P0 | 通过 |
| TC-019 | 线上启用绑定数 | 线上状态库 | 查询 enabled binding | 数量为 0 | P0 | 通过 |
| TC-020 | 服务日志 | systemd journal | 查看部署后日志 | 仅有页面/API 只读访问，无异常堆栈和 execute 写入 | P1 | 通过 |

## 回归范围
- 快速导航公共 JS 和顶吸样式。
- AI 自动规则调控拆页入口。
- 账号池、规则集、绑定关系、运行控制台、Token、日志页面基础加载。
- 本地规则匹配与状态库存储。
- 线上服务只读状态。
