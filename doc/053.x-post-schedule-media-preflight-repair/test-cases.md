# 测试用例

## 测试范围

material schedule 语言/FIFO、完整媒体预检、codec/dimensions repair、跨页补位、frozen-first、Relay、历史兼容和无真实 X 写入。

## 测试数据

全部使用本地 mock/fake；repair 输入分别注入 `invalid_media_codec` 和 `invalid_media_dimensions`。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | codec 自动重制 | 原视频 probe 返回 codec 错误 | 执行 material preflight | repair 一次，二次 probe 通过，冻结完整台账 | P0 | 通过 |
| TC-002 | dimensions 自动重制 | 原视频 probe 返回 dimensions 错误 | 执行 material preflight | repair 一次，二次 probe 通过，冻结完整台账 | P0 | 通过 |
| TC-003 | 语言跳过 | 新素材语言无目标账号 | 执行候选扫描 | 不下载该素材，后续同语言素材进入 preflight | P0 | 通过 |
| TC-004 | 跨页补位 | 第 50 条媒体失败，第 51 条正常 | 执行两页扫描 | 第 51 条建队；共享 repair_state | P0 | 通过 |
| TC-005 | frozen-first | 已存在队列 | 执行 schedule tick | 不访问源库、下载或 repair | P0 | 通过 |
| TC-006 | drama 不变 | drama due | 执行 schedule tick | 继续 deferred 行为 | P1 | 通过 |
| TC-007 | X 全量回归 | 本地完整代码 | discover test_x | 全部通过，允许条件 skip | P0 | 通过 |

## 回归范围

X account、pool、schedule/store、manual、catch-up、auto、repair、ledger、OAuth 和 UI 合同测试。
