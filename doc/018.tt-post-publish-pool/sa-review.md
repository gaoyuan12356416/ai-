# SA 评审意见

## 结论

有条件通过。架构和状态机可开发，但真实 Direct Post 必须默认关闭；审核、Intended Use、URL Property 和品牌片尾问题未解决前不得以运维方式绕开。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 合规 | 内部账号上传工具被 TikTok 官方列为不可接受用途 | 三重门禁默认关闭，只交付关闭态能力 | 已采纳 |
| SA-002 | P0 | 素材 | 新版片尾含 Logo、品牌和推广引导 | 真实 API 发布前提供合规替代或 TikTok 书面确认 | 已采纳 |
| SA-003 | P0 | Token | 快照表无 scope/App 审核信息 | 数据库候选与 creator info 实测分层展示 | 已采纳 |
| SA-004 | P0 | 幂等 | init 超时可能重复发帖 | 有 `publish_id` 只对账；结果不明进入 `needs_review` | 已采纳 |
| SA-005 | P1 | 视频 | 新版片尾含示例 ID | GPU 显示真实 ID并标记教程示例 | 已采纳 |
| SA-006 | P1 | 部署 | 主服务和 X sidecar 线上来自不同分支 | 在独立整合分支合并并分别回归 | 已采纳 |

## 决策记录

- CPU：页面、权限、快照只读、素材映射、队列、调度、审计。
- GPU：`/data` 成片、TikTok API 预检/发布/对账。
- CPU→GPU：loopback sidecar + SSH 反向隧道 + 专用 bearer；敏感 Token 使用 AES-GCM 短时信封且不落盘。
- 服务端媒体只使用 `PULL_FROM_URL`。
- 验收不创建真实 TikTok Post。

## PM 修订确认

2026-07-29 已将全部 P0/P1 建议写入需求和验收标准。
