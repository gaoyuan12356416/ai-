# SA 评审意见

## 结论

有条件通过。仅恢复 material schedule 完整预检；必须保持 frozen-first、最大可用子集、跨页 repair budget、历史不重放和 no-real-Post 验收。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | frozen recovery | 新逻辑可能在读取已有队列前下载/重制 | 继续先 query frozen plan，已有队列直接恢复 | 已采纳 |
| SA-002 | P0 | repair output | 只信 GPU 响应可能冻结错误媒体 | CPU 重下并核对 SHA/size/probe/profile | 已采纳 |
| SA-003 | P0 | historical rows | 自动重试 run 318 会有重复风险 | 历史 failed 永不自动重放 | 已采纳 |
| SA-004 | P1 | partial capacity | 单条坏素材可能重新形成全批阻塞 | candidate-local 失败继续 FIFO 深扫 | 已采纳 |
| SA-005 | P1 | drama | 同时恢复 drama 预检会改变既有 affinity/Relay 行为 | 本次只改 material schedule | 已采纳 |
| SA-006 | P1 | latency | 完整预检可能重新增加建队延迟 | 明确记录权衡；未来异步预热另立需求 | 已知风险 |

## 决策记录

- 用户本次明确选择预检识别并重制，覆盖 052 中 material schedule 的效率优先 deferred 决策。
- 覆盖范围不扩展到 drama schedule。

## PM 修订确认

需求已按评审限定范围与回滚边界，可开发。
