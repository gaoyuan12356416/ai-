# SA 测试用例评审

## 结论

通过。测试覆盖业务 URL、真实候选特殊字符、持久化幂等、媒体门禁、X v2 契约、未知结果防重、账号锁/刷新、内部鉴权及既有账号功能回归；真实 X/Post 与公网短链留作部署后验收。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | 真实素材名 | 候选素材名含 ASCII `[15]`，初版 URL 校验会拒绝 | 方括号按 URL 编码；增加 `5221348` 真实字段回归 | 已修复并通过 |
| TR-002 | 2xx + `errors[]` | 只检查 HTTP 状态可能把部分失败当成功 | 任何非空 `errors[]` 均 fail closed；Create Post 标记 unknown | 已修复并通过 |
| TR-003 | 视频规格 | 数据库时长不能代替真实媒体规格 | 下载后用 ffprobe 校验 H264/yuv420p/AAC-LC/逐行/时长/FPS/尺寸/比例 | 已覆盖 |
| TR-004 | 重复发布 | Create Post 没有业务幂等键 | 覆盖 published 重入零网络、unknown 重入拒绝、同键内容冲突 | 已覆盖 |
| TR-005 | Token 与内部路由 | 需证明过期刷新、disabled、payload 限制、响应脱敏 | 在账号测试加入对应 mock 与本地 HTTP handler 回归 | 已覆盖 |

## QA 修订确认

2026-07-23：`test_x_posts` 14/14、`test_x_accounts` 32/32、App contract 5/5、owner backfill 4/4；共 55 项通过，0 失败，0 阻塞。
