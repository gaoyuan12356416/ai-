# 010.ad-control-v3-ui-usability 需求与技术设计

## 背景

V3 已支持 FB 三层规则、观察/正式执行、暂停和复制，但 2026-07-17 生产浏览器验证发现：管理员优化师下拉一次渲染 394 条且不可搜索；页面串行加载公共壳与 `/meta`；产品目录只有 15 个报表口径枚举；范围估算位于第 1 步却强制依赖第 2 步对象层级；对象页存在无决策价值的“当前层级能力”；“保存并立即试算”成功后仍停留编辑器。

## 目标

1. 优化师、列表产品筛选均支持搜索，关闭状态不渲染全部选项。
2. 首屏并行加载公共壳和元数据，服务端对共享元数据做 15～300 秒短缓存。
3. 将短剧产品细化到具体投放产品；以已审核 FB App ID 批量同步同组 App/W2A 产品。
4. 估算入口移到对象层级选择之后，输入齐全即可执行。
5. 删除“当前层级能力”展示，不删除规则条件页实际可用字段目录。
6. 保存并立即试算成功后关闭编辑器、返回规则组列表；保存或试算失败时保留编辑现场。

## 范围

### 包含

- V3 规则组页和执行日志页动态 UI。
- `/api/ad-control/v3/meta` 共享选项加载性能。
- `ads_ai.ad_control_v3_product_catalog` 追加具体投放产品，不新建业务表。
- Dramawave FB App ID `1031273318485141` 的索引等值批量同步；2026-07-17 实查 129 条平台配置。
- 具体产品选择值采用 `app:<ads_apps_setting.id>` 或 `w2a:<landing_id>`；展示名来自平台产品/W2A 信息。
- 扫描时将具体产品解析为受审核的报表 `product` 等值范围，再叠加 `app_id/w2a_page_id` 身份谓词。
- 原 15 个宽口径报表产品继续可读、可执行，保证历史规则兼容。

### 不包含

- TikTok 调控、V2 UI 改造、账户/账户池范围回归。
- 修改 Meta 暂停/复制执行语义、created_data 写入协议。
- 自动识别所有未知 App ID 的产品类型；每个产品族仍需审核后同步。

## 用户故事 / 业务规则

- 管理员可输入优化师姓名、邮箱或 ID 搜索并选择；普通优化师仍锁定本人。
- 产品选择支持搜索具体 App/W2A；选择具体 W2A 只扫描该 `w2a_page_id`，不是仅改标签。
- 同一规则组不可同时选择一个宽口径产品及其重叠的具体产品，避免重复聚合；服务端返回 `overlapping_product_scope`。
- 产品同步默认只读；写入必须同时给出预期条数和计划 hash，源数据漂移则拒绝。
- 元数据缓存只缓存产品、时区和管理员有效优化师，返回深拷贝；不缓存用户授权结论、规则或日志。
- 估算只读源数据，不写 Meta；保存试算仍遵循单飞、CAS 和停用安全默认。

## 交互与流程

1. 第 1 步填写名称、渠道、优化师、产品、可选时区。
2. 管理员点击优化师控件后输入关键词，选中一项；列表与日志筛选同样可搜索。
3. 第 2 步选择 Campaign/Ad Set/Ad；同页填写指标窗口并点击“估算当前范围”。
4. 第 3～5 步配置规则并检查。
5. 点击“保存并立即试算”：先保存，随后试算；两者均成功才回到列表。

## 技术设计

### 影响模块

- `features/ad_control_v3/assets/app.js|app.css`：搜索单选、步骤重排、成功导航、并行初始化、产品显示名。
- `features/ad_control_v3/service.py`：共享元数据 TTL 缓存、产品目录范围解析。
- `features/ad_control_v3/channels/facebook.py`：具体产品有界查询和身份映射。
- `scripts/sync_ad_control_v3_delivery_products.py`：App ID 批量同步工具，默认 dry-run。
- `ads_ai.ad_control_v3_product_catalog`：只追加/更新审核后的具体产品行。

### 数据结构

沿用既有表，不做 DDL。具体产品仍写 `product_value`，附加信息写入既有 JSON：

```json
{
  "product_value": "w2a:1723",
  "canonical_product": "Dramawave",
  "source_app_ids": [2477],
  "evidence": {
    "catalog_kind": "delivery_product",
    "platform_app_id": "1031273318485141",
    "scope": {
      "insight_products": ["Dramawave"],
      "insight_app_ids": ["[w2a]drama-double"],
      "w2a_page_ids": [1723]
    }
  }
}
```

### API / 接口

- 现有接口路径和公开请求字段不变。
- `/meta.products[]` 继续返回既有字段，`evidence.display_name/catalog_kind` 为兼容性扩展。
- `delivery_product_scopes` 仅为 Service 到 Adapter 的内部字段，HTTP allowlist 不接受客户端提交。
- 新配置：`AD_CONTROL_V3_META_CACHE_TTL_SECONDS`，默认 60，边界 15～300。

### 异常与边界

- 具体产品证据缺失、引用停用报表产品或无 App/W2A 身份：`product_catalog_invalid`，503 失败关闭。
- 宽/细产品范围重叠：`overlapping_product_scope`，不执行查询。
- 一个对象聚合到多个具体投放产品或上下文截断：记录 `ambiguous_product_scope/context_aggregation_truncated`，不执行动作。
- 同步源条数或 hash 与审核值不一致：事务前拒绝写入。
- 试算失败不返回列表，保留已保存规则与错误提示，允许修正后重试。

## 验收标准

- 394 个优化师场景可按姓名/邮箱/ID 搜索；首屏 HTML 不再含 394 个原生 option。
- 公共壳与 `/meta` 并行；连续两次 `/meta` 只调用一次共享 loader，缓存对象不可被调用方污染。
- Dramawave 同 App ID 129 条源配置全部生成唯一具体选择值，源/目标数量与计划 hash 回读一致。
- 具体 W2A SQL 保留 `dpdo`、data_source/platform/product/date/optimizer、8 秒熔断和绑定参数，并包含 `w2a_page_id` 限定。
- 第 1 步不显示估算，第 2 步选层级后可估算；页面无“当前层级能力”。
- 保存+试算成功后列表可见；失败时不丢编辑数据。
- V3、V2、真实暂停/复制回归无倒退，Meta 写开关不因本次 UI 发布改变。

## 风险与待确认

- 129 条为 2026-07-17 当前快照；后续新增 W2A 需再次以 dry-run/hash 同步。
- 具体产品底层报表枚举采用审核映射，不能把平台名称直接用于无界模糊查询。

## 变更记录

- 2026-07-17：建立需求，完成生产只读复现和 App ID 129 条取证。
