# AI自动规则调控前端重设计 需求与技术设计

## 背景
当前 AI 自动规则调控前端虽然已经拆成多个页面，但信息架构仍偏“数据表 CRUD”，运营创建一个跨区调控方案时需要在规则集、账户池、绑定、运行控制台之间来回跳转，理解成本高，也不符合“按需创建规则/账户池/绑定关系”的操作预期。

本次先做前端重设计评审，不直接改线上页面、不触发广告动作。

## 目标
- 重新按运营工作流组织页面：先创建调控方案，再进入专家配置和运行审计。
- 让“+8 账户跑跨区国家组关停”成为一条可理解、可保存、可 preview 的方案，而不是散落的 JSON 表单。
- 保留已有安全约束：新方案默认 disabled，真实执行必须先 preview。
- 保留公共 AI 后台 shell：`quick-nav.js`、`ui-topbar.css`、`ui-topbar.js`。

## 范围

### 包含
- 新增前端原型：`prototype/ad-control-redesign-preview.html`。
- 设计新的导航信息架构。
- 设计“创建调控方案”向导页面。
- 设计专家配置入口：账户池、规则库、绑定策略、Token 与权限。
- 设计运行控制台和执行日志在新工作流中的位置。

### 不包含
- 不改线上页面。
- 不改后端 API。
- 不部署。
- 不执行 Meta preview/execute。

## 用户故事 / 业务规则
- 运营进入调控中心后，优先看到“创建调控方案”，按场景完成配置。
- 运营选择跨区场景后，可以一次性配置：产品、账户时区、国家组、规则阈值、关闭窗口、重启策略。
- 系统保存时仍按现有架构生成单产品账户池、规则集和绑定关系。
- 专家可以进入账户池、规则库、绑定策略、Token 与权限页面做细节维护。
- 运行人员进入运行控制台做 live preview、dry-run、真实确认执行和急停。

## 交互与流程
1. 选择调控场景：默认突出“+8 账户跑跨区国家组自动关停”。
2. 选择产品与账户范围：
   - 产品使用 `label` 展示，`product` key 作为值。
   - 账户按产品分组，支持 +8 时区筛选。
   - 国家组使用 `ads_facebook_auto_created_data.country`，如 `WW-4`、`WW-0`、`JUWW`。
3. 配置规则阈值：
   - 用可视化规则行展示条件，不默认暴露 JSON。
   - 支持 pause 和 observe。
4. 配置绑定策略：
   - 关闭窗口。
   - 执行时区。
   - 当天禁止重启。
   - 隔天允许按规则重启。
5. 生成只读 preview：
   - 展示账户、产品、campaign、国家组、运行时长、实时表现、命中规则、动作。
6. 保存方案：
   - 默认 disabled。
   - 生成产品级账户池、规则集、绑定关系。

## 技术设计

### 影响模块
- 评审阶段仅新增 `doc/005.../prototype/ad-control-redesign-preview.html`。
- 后续实现预计影响：
  - `static/ad-control*.html`
  - `static/ad-control-pages.js`
  - `static/ad-control-pages.css`
  - `static/navigation.json`
  - 如需保存方案草稿，可能新增少量后端 API 或复用现有绑定 API。

### 数据结构
原型阶段无数据库变更。

后续实现优先复用现有结构：
- `ad_control_account_group`：账户池。
- `ad_control_rule_set`：规则集。
- `ad_control_rule_group`：绑定策略。
- `strategy_json`：关闭时间、执行时区、同日禁止重启、隔天允许重启、国家组。

### API / 接口
原型阶段无接口变更。

后续实现优先复用：
- `GET /api/ad-control/products`
- `GET /api/ad-control/accounts`
- `POST /api/ad-control/account-groups`
- `POST /api/ad-control/rule-sets`
- `POST /api/ad-control/bindings`
- `POST /api/ad-control/bindings/{id}/preview-live`

### 异常与边界
- 不允许把多个产品保存成一个绑定；多产品仍批量生成单产品配置。
- 不按 campaign 命名判断国家组。
- 不展示明文 token。
- 新方案默认 disabled。
- Preview 之前不允许真实 execute。

## 验收标准
- 用户能一眼看出“先建方案，再运行 preview/execute”的主流程。
- 跨区配置不需要直接编辑 JSON。
- 产品、账户、国家组、规则、绑定策略在一个方案视角下可理解。
- 专家配置页仍可单独维护底层对象。
- 页面保持 AI 后台公共顶吸和快速导航风格。
- 不产生任何广告动作。

## 风险与待确认
- 是否新增 `/ad-control-plans.html` 作为“创建调控方案”页面，还是复用 `/ad-control.html` 作为工作台入口。
- 规则可视化编辑是否需要支持全部操作符，还是 v1 只支持常见阈值条件。
- 方案草稿是否需要后端持久化，还是保存时直接生成账户池/规则集/绑定。

## 变更记录
- 2026-07-01：创建前端重设计需求和静态原型，等待评审。
