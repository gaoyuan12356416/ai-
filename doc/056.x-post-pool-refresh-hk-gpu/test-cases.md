# 测试用例

## 测试范围

标签规则、违规兼容、语言容量、Premium relay、GPU worker/COS、状态冻结与删除边界。

## 测试数据

- 离线 fixture：unsafe source/resource tag、EN/JA 账号与候选、长短视频、修复响应。
- 生产冻结：保留 220 条、deferred 5 条、删除 3 条；不含真实发布动作。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 标签不再过滤 | source/resource tag 命中旧词库 | 运行新旧 selector | 素材被选择且无 `resource_tags` gate 查询 | P0 | 通过 |
| TC-002 | 无语言账号 | 只有 EN 账号、JA 素材在前 | 执行预检 | JA 写未排期，EN 正常选中 | P0 | 通过 |
| TC-003 | 语言容量已满 | EN/JA 各一账号，两个 JA 候选 | 执行预检 | 第二个 JA 不写错误，EN 继续选中 | P0 | 通过 |
| TC-004 | Premium relay | 目标非 Premium、同语言 relay 可用 | 预检 140 秒以上视频 | 冻结为 `premium_relay_repost` | P0 | 通过 |
| TC-005 | 无 relay | 同语言无可用 Premium | 预检长视频 | 仅该素材返回 Premium 错误，零发布 | P0 | 通过 |
| TC-006 | 香港 worker 启动 | Python/COS/ffmpeg 配置完成 | 检查本机 health 与错误 token | 正确 token health，错误 token 被拒绝 | P0 | 通过 |
| TC-007 | NVENC/COS 回填 | 8 个显式素材 ID | 运行 backfill | 逐条修复/验证并更新池状态，零队列 | P0 | 通过，8/8 |
| TC-008 | 历史状态刷新 | 冻结指纹未漂移 | 当前源/账号能力确认后事务更新 | 212 条清空，无越界 | P0 | 通过，212/212 |
| TC-009 | 精确删除 | 3 条未发布无占用 | 在备份后事务删除 | 仅 ID 86/296/297 被删 | P0 | 通过，3/3 |
| TC-010 | deferred 保护 | 5 条未到投放时间 | 前后对账 | 仍为 deferred 且数量为 5 | P0 | 通过，5 条未改 |
| TC-011 | 零真实发布 | 记录队列/日志基线 | 完成全部验收后复核 | 队列与发布日志未增加 | P0 | 通过，627/627 |
| TC-012 | 长视频回填能力 | 180 秒源视频本身合规 | 默认显式回填 | 以 Premium 14400 秒上限探测，不误报会员错误、不调用 GPU | P0 | 通过 |
| TC-013 | 显式强制重制 | 源视频当前探测合规 | `--force-repair` 回填 | 仍调用 GPU，原因是 `operator_forced_repair`，新 job key、COS 二次校验、零队列 | P0 | 通过 |

## 回归范围

X daily/schedule/material pool/random relay/media repair/backfill 全套脚本测试，生产 sidecar 健康、timer 状态、SQLite integrity check 与三端 systemd 状态。
