# 013.x-post-media-repair SA 评审

## 结论

方案通过。修复点位于 selector 合规之后、daily plan 之前；GPU 只处理确定性的媒体规格问题，CPU 对返回对象执行完整二次校验，原素材身份和既有发布事务不变。

## 评审项

| 编号 | 级别 | 问题 | 决策 | 状态 |
| --- | --- | --- | --- | --- |
| SA-001 | P1 | 现有 GPU `/render` 单文件会 concat-copy，不能保证转 H264/缩放 | 新建独立 X repair worker，不复用剧集接口契约 | 已关闭 |
| SA-002 | P1 | queue 后替换媒体会破坏冻结指纹和 unknown 语义 | 只允许 plan 前修复；发布阶段保持只读重验 | 已关闭 |
| SA-003 | P1 | 修复文件可能被当成新素材绕过排重 | `material_key/material_id/pool_item_id` 永远保留原值 | 已关闭 |
| SA-004 | P1 | CPU 与 GPU 两次下载间源对象可能变化 | 双端比较 source SHA/size，不一致失败关闭 | 已关闭 |
| SA-005 | P1 | GPU 公网接口扩大攻击面 | worker 绑定 127.0.0.1，CPU 经 SSH `-R` 回环端口访问并使用独立 Bearer | 已关闭 |
| SA-006 | P1 | worker 回执或 COS 对象可被替换 | COS 校验后 CPU 重下载并核对 SHA/size，再走正式 probe | 已关闭 |
| SA-007 | P2 | 极端宽高比若直接拉伸会损坏内容 | 固定画布等比缩放+补边，不裁剪、不拉伸 | 已关闭 |
| SA-008 | P2 | 无音轨素材修复后仍过不了 X probe | worker 注入等长 AAC-LC 静音轨 | 已关闭 |
| SA-009 | P2 | repeated failed_preflight 重复耗 GPU | job、COS key、manifest content-addressed 幂等；并发 flock | 已关闭 |
| SA-010 | P2 | 同批大量坏素材拖垮 90 分钟 oneshot | 每批最多修复六条，失败继续 FIFO，最终不足三条零 queue | 已关闭 |
| SA-011 | P2 | GPU 当前剧集目录不是 Git 工作树 | 新 worker 部署到 `/opt/x-post-media-repair/releases/<commit>`，不覆盖旧快照 | 已关闭 |
| SA-012 | P2 | 回滚误恢复 SQLite/Token 可丢真实发布历史 | 回滚只切代码和服务，保留新增列、SQLite、Token、COS 对象 | 已关闭 |

## 边界确认

- 合规、违规、素材身份和危险标签仍由 selector 负责，GPU 不重新判定业务合规。
- codec/dimensions/超长 duration 可修复；仅超长 duration 固定裁尾至 139 秒。过短、非有限 duration、frame-rate、scan、download 等错误保持不可用。
- 当前九条均未绑定 queue，可安全修复；今日账号已有真实发布记录，禁止再次发帖。

## 2026-07-29 增量评审

- profile 升级为 `x-h264-nvenc-720-trim139-v2`，job key namespace、COS 路径和 manifest 同步升版，禁止复用 v1 产物。
- 短剧 `validation_failed -> pending` 只允许精确旧错误、集数、未绑定、无历史的成功重验；先 dry guard，后 GPU 修复、COS HEAD、CPU 重下载/SHA/size/probe，最后同事务清错。
- 恢复命令不包含建计划或发布调用；10:06 `failed_preflight` 保持终态，账号粘性、FIFO、全量候选原子建队列规则不变。
