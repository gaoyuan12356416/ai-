# AI自动规则调控跨区手动配置优化 需求与技术设计

## 背景
AI 后台已经上线独立的 `AI自动规则调控` 模块。当前追加需求是让运营可以手动配置“+8 账户跑北美/跨区国家组”的调控策略，而不是把所有配置堆在一个页面，也不是直接启用自动 runner。

业务口径：
- 控制对象仍为 Meta/Facebook campaign。
- 表现数据在 preview/execute 时从 Meta 实时读取。
- campaign 起始时间按 `campaign_id` 从 insight 表最早记录读取并缓存 Redis。
- 跨区判断只看账户时区和业务资产表国家组，不按 campaign 命名猜测。

## 目标
- 前端按职责拆页：概览、规则集、账户池、绑定关系、运行控制台、Token 配置、执行日志。
- 支持手动创建“+8 跨区关停”配置：产品多选、+8 账户筛选、国家组、关闭时间、当天禁止重启、隔天允许重启。
- 新增字段可以被规则匹配：`account_time_zone`、`country`。
- 保持安全边界：默认不启用新绑定、不启动自动 runner、不产生广告动作。

## 范围

### 包含
- `static/ad-control-pages.js` 前端跨区向导与拆页能力。
- `static/ad-control-pages.css` 页面样式。
- `static/navigation.json` 和 `static/quick-nav.js` 的 AI 自动规则调控分组与子入口。
- `app.py` 后端规则集、账户池、绑定、策略 JSON、preview item 补充字段和规则匹配支持。
- 线上只读/本地冒烟/浏览器 DOM 验证。

### 不包含
- 不启用自动定时 runner。
- 不做真实 Meta 关闭/重启动作测试。
- 不修改 `AI自动化投放` skill。
- 不按 campaign 命名判断 `ww-4`、`ww-0`、`juww`。

## 用户故事 / 业务规则
- 运营可以在账户池页选择多个产品，加载各产品账户，按 `+8` 时区筛选并批量保存单产品账户池。
- 运营可以在规则集页选择常用国家组，如 `WW-4`、`WW-0`、`JUWW`，生成可视化规则 JSON。
- 运营可以在绑定关系页批量生成“产品 + 账户池 + 规则集”的绑定，并保存关闭时间、执行时区、当天禁止重启、隔天允许重启策略。
- 新绑定默认 disabled，必须人工启用后后续 runner 才可使用。
- 运行控制台仍要求先 preview，再确认执行；本次系统测试不执行真实关闭。

## 交互与流程
1. 进入 `/ad-control.html` 查看模块概览。
2. 在 `/ad-control-account-pools.html` 创建 +8 账户池。
3. 在 `/ad-control-rules.html` 创建跨区关停规则集。
4. 在 `/ad-control-bindings.html` 绑定产品、账户池、规则集和执行策略。
5. 在 `/ad-control-run.html` 选择绑定做 live preview 或 dry-run。
6. 在 `/ad-control-logs.html` 查看 preview/execute 审计。
7. 在 `/ad-control-tokens.html` 维护产品默认 token 和账户 override token。

## 技术设计

### 影响模块
- `app.py`
- `static/ad-control-pages.js`
- `static/ad-control-pages.css`
- `static/quick-nav.js`
- `static/navigation.json`
- `codex-personal-skills/skills/ai-auto-rule-control/SKILL.md`

### 数据结构
- `ad_control_rule_set`：可复用规则集。
- `ad_control_account_group`：账户池。
- `ad_control_rule_group`：绑定关系，新增/使用：
  - `rule_set_id`
  - `strategy_json`
  - `enabled`
  - `emergency_stopped`
- `ad_control_action`：preview/execute/dry-run 审计。

### API / 接口
- `GET /api/ad-control/products`
- `GET /api/ad-control/accounts?product=...`
- `GET/POST/PUT/DELETE /api/ad-control/rule-sets`
- `GET/POST/PUT/DELETE /api/ad-control/account-groups`
- `GET/POST/PUT/DELETE /api/ad-control/bindings`
- `POST /api/ad-control/bindings/{id}/preview-live`
- `POST /api/ad-control/bindings/{id}/execute-live`
- `GET /api/ad-control/actions`
- `POST /api/ad-control/campaign-start/refresh`
- `GET /api/ad-control/runner/status`

### 异常与边界
- 无 token、token 无权限、缺 campaign 起始时间、campaign 不在产品白名单时跳过并写审计。
- `country` 来自 `ads_facebook_auto_created_data.country`。
- `account_time_zone` 来自 `ads_accounts_setting.time_zone`。
- 没有起始时间时不兜底资产创建时间，避免误关。
- 当天禁止重启策略当前只保存和展示，真实自动隔天重启留给后续 runner。

## 验收标准
- 七个页面都加载公共 `ui-topbar.js`、`quick-nav.js` 和统一样式。
- 快速导航配置能看到 `AI自动规则调控` 下七个子入口。
- 产品多选、+8 账户筛选、国家组、跨区规则模板、绑定策略字段可见且可保存。
- 后端规则匹配支持 `account_time_zone` 和 `country`。
- 线上部署后 `ad_control_action` 计数不增加，启用绑定数为 0。
- 未做真实 execute 的情况下不影响线上广告。

## 风险与待确认
- 未做登录态下完整 CRUD 浏览器端人工流，只做了页面 DOM、公共资源、本地后端冒烟和线上只读检查。
- 未调用真实 Meta live preview/execute，避免测试污染线上广告。
- 后续如果启用自动 runner，需要补充同日禁止重启和隔天重启的端到端测试。

## 变更记录
- 2026-06-30：按跨区手动配置方案完成系统测试与标准流程文档。
