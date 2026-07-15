# 测试用例

## 测试范围

公平选批、Meta 写前安全检查、限流熔断、续跑状态机、MySQL 日志 CRUD/回退、历史回填、列表性能、前端展示、部署补丁与线上健康回归。

## 测试数据

- 构造 3 个账户各 30 条和 250 个账户各 1 条的目标集。
- 构造 Meta `code=4 / subcode=5044001`、owner 缺失、永久配置错误和终态跳过。
- 生产历史 SQLite `ad_control_action`（只做回填，不触发 Meta）。
- 生产启用规则组 `frg_plus8_non_asian_lang_10am_dramawave_binding`。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-01 | 公平选批 | 3账户各30条 | 选择 max=200/per=20 | 共60条，各账户20，顺序确定 | P0 | 通过 |
| TC-02 | 全局上限 | 250账户 | 选择一批 | 总数200 | P0 | 通过 |
| TC-03 | code4分类 | 非 transient error | 解析错误 | retryable/rate_limited=true，保留code/subcode | P0 | 通过 |
| TC-04 | 应用级熔断 | 4条、worker=1 | 首条返回code4 | 仅1次Graph写，1失败+3 deferred，remaining=4 | P0 | 通过 |
| TC-05 | owner缺失 | Graph无account_id | 正式执行 | 不调用PAUSED，记blocked | P0 | 通过 |
| TC-06 | owner不一致 | Graph账户不同 | 正式执行 | fail closed，记account_owner_mismatch | P0 | 待线上dry-run |
| TC-07 | 终态跳过 | not_active | 汇总 | run_status=executed，不续跑该目标 | P1 | 通过 |
| TC-08 | 受阻跳过 | missing_meta_token | 汇总 | run_status=blocked | P0 | 通过 |
| TC-09 | 新事件次数重置 | 上一事件partial attempt=9 | 传入新event_key | attempt=1 | P0 | 通过 |
| TC-10 | 跨午夜续跑 | 23:55 partial | 次日运行 | 保留原event_key并继续 | P1 | 通过 |
| TC-11 | 上限后零目标验收 | attempt达到上限 | fresh preview=0 | 先写verification并完成，不误报超限 | P0 | 通过（结构验证） |
| TC-12 | MySQL时间序列化 | DATETIME对象 | 列表解码并JSON dumps | 返回标准字符串，无500 | P0 | 通过 |
| TC-13 | 敏感字段脱敏 | criteria/results含token/password | 写日志 | 值变为REDACTED | P0 | 通过 |
| TC-14 | 迁移幂等 | action已在ads_ai | 二次执行migration | 默认跳过，不覆盖runner状态 | P0 | 待线上回填复核 |
| TC-15 | MySQL失败回退 | 暂时不可写 | 执行/列表 | Meta计数不变，SQLite可读且页面提示 | P1 | 代码审查通过 |
| TC-16 | 列表轻量化 | 每行results 200条 | GET actions | 不从MySQL/SQLite读取results_json | P1 | 代码审查通过 |
| TC-17 | 明细懒加载 | 日志列表已打开 | 展开targets/raw | 单卡只发一次详情请求 | P1 | 通过 |
| TC-18 | 缓存版本 | 7个HTML | 检查引用 | JS/CSS均为20260715log1 | P1 | 通过 |
| TC-19 | 日期边界 | 上海自然日 | 查询date_from/to | 后端换算UTC边界 | P1 | 待线上API复核 |
| TC-20 | 生产窄补丁 | 同源复合app快照 | 应用两次并compile | 第一次changed、第二次unchanged | P0 | 通过 |
| TC-21 | 在线健康 | 部署完成 | health/service/log/API | 服务active、接口200、无新traceback | P0 | 待部署 |

## 回归范围

- 规则组列表、账户池、绑定关系、token 页面仍能加载。
- `ad_control_center` 权限检查不变。
- live preview hash/confirm/whitelist/ACTIVE/owner 安全门不放宽。
- 未修改其它 AI 后台模块与服务。
