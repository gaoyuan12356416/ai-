# 012.ad-control-v3-multi-optimizer 需求与技术设计

## 背景

V3 规则组页面会将普通登录用户解析为优化师。生产账号王鹏（用户 `22fa4aa4`、邮箱 `peng.wangg@yingliangads.com`）通过同一强身份邮箱命中优化师 `387` 与 `686`；旧实现要求恰好命中一个优化师，因此 `/api/ad-control/v3/meta` 返回 409，页面被安全阻断。业务确认 `686` 是王鹏的小号，两个优化师范围都应生效。

## 目标

- 允许同一普通用户通过精确 `user_id` 或精确邮箱绑定多个有效优化师别名。
- 王鹏进入 V3 后同时看到并使用 `387`、`686` 范围。
- 新建/更新规则组后，两个优化师对应的广告都进入筛选、观察、暂停和复制链路。
- 保持名称模糊命中、越权 optimizer_id、跨别名重复对象等场景安全失败。
- 不触发测试性 Meta 写操作；上线验证仅做身份、列表和只读范围检查。

## 范围

### 包含

- 身份解析、规则组多优化师作用域、候选扫描合并、UI 展示和执行日志。
- `ads_ai.ad_control_v3_rule_group_optimizer` 多对多关联表及旧规则回填。
- 复制 intent/lineage 记录实际命中目标的优化师 ID。

### 不包含

- 修改 Feishu 登录账号或 `kunlunads_dev` 身份数据。
- 合并/删除优化师 387、686。
- TikTok 调控、产品枚举、Meta 复制逻辑本身的改版。
- 以生产广告做暂停或复制验证。

## 用户故事 / 业务规则

1. 普通用户的候选来自第一个非空的精确身份层：`user_id`、邮箱、姓名。
2. 只有 `user_id` 或邮箱精确命中允许返回多个优化师；姓名层多命中仍返回 409。
3. 普通用户不能在请求中指定其别名集合之外的优化师。
4. 规则组保留最低 ID 作为兼容主优化师，同时将完整集合写入关联表。
5. 扫描对每个优化师分别执行既有精确查询并合并；同一账号/层级/对象跨别名重复时标记 `ambiguous_optimizer_scope`，不得执行两次。
6. 新执行目标、复制 intent 与 lineage 使用实际命中对象的 optimizer_id。
7. 单用户最多绑定 20 个优化师，合并后候选最多 20,000 个。

## 交互与流程

- 页面顶部优化师信息显示多个名称与 ID，并明确“同时生效”。
- 规则列表、检查页和执行日志显示规则组完整优化师范围。
- 普通用户仍不能编辑优化师；管理员保持单选优化师规则。

## 技术设计

### 影响模块

- `features/ad_control_v3/catalog.py`：强身份多别名解析。
- `service.py`：权限、扫描合并、API 元数据和执行日志。
- `repository.py`：多对多关联的事务写入、查询和权限过滤。
- `live_execution.py`：实际优化师审计归属。
- `assets/app.js`：多优化师展示与作用域指纹。

### 数据结构

新增 `ads_ai.ad_control_v3_rule_group_optimizer(rule_group_id, optimizer_id, is_primary, created_at)`；主键为 `(rule_group_id, optimizer_id)`，反向索引为 `(optimizer_id, rule_group_id)`，外键只指向 V3 规则组表。迁移使用 `INSERT IGNORE ... SELECT` 将所有现存规则主优化师回填为关联行。

### API / 接口

- `GET /api/ad-control/v3/meta` 新增 `actor.optimizer_ids`、`permissions.current_optimizer_ids`，`optimizers` 可返回多个锁定项。
- 规则组响应新增 `optimizer_ids`；执行日志响应新增 `optimizer_ids`、`optimizer_names`。旧 `optimizer_id`/`optimizer_name` 保留。
- 写接口不接受客户端直接提交 `optimizer_ids`，由服务端身份解析生成。

### 异常与边界

- `optimizer_identity_ambiguous`：姓名层或无可信层的多命中。
- `optimizer_identity_too_large`：别名超过 20。
- `optimizer_forbidden`：普通用户伪造范围外 optimizer_id。
- `ambiguous_optimizer_scope`：同一 Meta 对象跨别名重复，记录但不执行。
- 关联表不存在或迁移不完整时停止发布，不让新代码启动。

## 验收标准

- 王鹏 meta 返回 200，优化师集合严格为 `[387, 686]`。
- 王鹏规则列表返回 200；新规则组完整保存两个关联 ID。
- 试算分别按 387、686 扫描，目标保留实际 optimizer_id。
- 其他单优化师用户行为不变；姓名冲突仍阻断。
- 旧规则组回填数量与规则组数量一致。
- V3 全量自动化回归、Python 3.9 语法、JS 语法、迁移静态审计全部通过。
- 线上验证不产生 Meta 写请求。

## 风险与待确认

- 若同一 Meta 对象被两个别名同时归属，当前选择阻断而非任取一方，需人工清理上游归属。
- 历史执行日志通过规则组当前关联范围授权；本次不重写历史日志。
- 生产只确认王鹏 387/686 当前七日对象无重叠；后续仍由运行时去重保护。

## 变更记录

- 2026-07-17：业务确认 686 为王鹏小号，两个优化师都必须生效。
