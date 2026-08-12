# SA 评审意见

## 结论

有条件通过。必须采用“不可变模板版本 + 双 GPU 通道”方案；不得把模板选择实现为生产
全局 env 开关。下列问题均已纳入需求与开发计划。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-01 | P0 | 执行路由 | 单 GPU mode 无法安全支持不同模板同时选择 | 为 direct-outro 新增独立实例与隧道，执行器按冻结模板版本选 client | 已接受 |
| SA-02 | P0 | 历史兼容 | 旧配置没有新字段，若默认不明确会改变现有模板 | 缺字段在校验、UI 和执行三处统一默认 `random_overlay` | 已接受 |
| SA-03 | P0 | 重试/核对 | 只在 prepare 选路由会导致 publish/reconcile 访问错误 manifest | 三阶段使用同一 `_video_route_for_task` | 已接受 |
| SA-04 | P1 | 配置安全 | 任意 profile/URL 若由浏览器传入会扩大攻击面 | 浏览器只传枚举；profile/trim/URL 均为服务端固定合同 | 已接受 |
| SA-05 | P1 | 运行隔离 | 两个 GPU 进程共用端口/work root 可能冲突 | 新实例使用 GPU 8832、CPU 18834、独立 work root | 已接受 |
| SA-06 | P1 | 验收安全 | 真发布 canary 会产生不可逆外部帖子 | 仅测试、health、离线合同、自然调度证据和页面只读验收 | 已接受 |

## 决策记录

- 配置键：`video_template`。
- 枚举：`random_overlay`、`direct_outro`。
- 默认：`random_overlay`。
- direct-outro profile/trim：`tt-post-direct-outro-hevc-720x1280-v2` / `4.333333`。
- direct-outro CPU loopback：`127.0.0.1:18834`；GPU loopback：`127.0.0.1:8832`。
- 不做数据库 DDL，不回填历史 config JSON。

## PM 修订确认

已将 SA-01 至 SA-06 全部写入 `requirements.md` 的技术设计、异常边界、验收和风险章节。
