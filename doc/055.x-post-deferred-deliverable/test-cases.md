# 测试用例

## 测试范围

素材入池统计、派生状态、内部候选复检、FIFO 事务校验、HTTP 参数、页面展示、历史
记录兼容和无真实发帖生产验收。

## 测试数据

- 固定当前时间和未来/相等/过去 `deploy_time` 的 selector fixture。
- SQLite 临时库中的未绑定 `unpublished` 素材及历史错误码。
- 生产只读目标：池 ID 843、844、845、847、848。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 入池遇到未来时间 | validation check 为 `drama_not_yet_deliverable` | 添加素材并查询 | `deferred_count=1`、available=0、failed=0，行状态 deferred | P0 | 通过 |
| TC-002 | 历史等待行重入候选 | 旧行错误码为 future-time、无队列 | 调用 available candidate query | 该行被返回供 selector 复检 | P0 | 通过 |
| TC-003 | 边界前临时跳过 | 新于正常素材的 future 行本次已重检 | 事务核对最大可用子集 | 允许跳过 future 行并选择后续正常行 | P0 | 通过 |
| TC-004 | 旧检查证据不可跳过 | future 行未在本次预检刷新 | 尝试越过并冻结后续素材 | 返回 FIFO conflict、零新队列 | P0 | 通过 |
| TC-005 | 到时成功恢复 | selector 当前通过、prepared 包含旧行 | 原子冻结队列 | 同事务清空旧错误并绑定队列 | P0 | 通过 |
| TC-006 | UI 状态 | API 行为 deferred | 渲染列表/筛选/提示 | 显示“待可投放”，不显示红色“不可用” | P1 | 通过 |
| TC-007 | API 参数 | availability=deferred | 主 API 与 Sidecar 查询 | 均接受且只返回等待行 | P1 | 通过 |
| TC-008 | 其他错误不放宽 | material_not_found 等确定性失败 | 查询/调度 | 仍为 validation_failed，不进入非法发布 | P0 | 通过 |
| TC-009 | 发布历史不重用 | future-time 行已有队列/未知结果 | 候选查询 | 永不返回自动候选 | P0 | 通过 |
| TC-010 | 生产无发帖验收 | 上线前冻结 queue/log/Post/unknown 计数 | 部署、健康检查、自然 timer 观察 | 无部署触发的真实 X 写；历史行得到新复检证据或等待下个自然槽 | P0 | 通过 |
| TC-011 | 到点证明不可伪造 | 旧行仍为 deferred，候选携带未来 deploy_time | 尝试原子建队列 | FIFO conflict、零队列、仍为 deferred | P0 | 通过 |
| TC-012 | 错误目录完整性 | 扫描 8 个发布链路模块的稳定 literal error code | 对照错误目录 | 除集中说明的字段级总码外全部逐码出现 | P1 | 通过 |

## 回归范围

- 既有素材池 add/list/delete、自动去重、非阻断错误、媒体可复检错误。
- 多排期素材 selector、partial capacity、random pairing、Premium relay。
- 主 API Cookie/导航权限和未知 availability 拒绝。
- 页面内联 JavaScript 语法与 DOM 安全。
