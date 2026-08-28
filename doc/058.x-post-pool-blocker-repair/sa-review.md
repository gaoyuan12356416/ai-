# SA需求评审

2026-08-28，代码/生产只读核查完成。

- 确认两类根因独立：素材全配置unknown fence；短剧deferred只probe无repair导致绑定持续占用。
- 修正范围设计为configured/eligible/prepared，防止foreign_owner与语言容量证据误拦。
- 保留短剧自身unknown全池保护；不把恢复任务扩成解除所有安全检查。
- 媒体修复移到credential context前，之后重验Token，避免长GPU准备造成上传时Token过期。
- 历史q533/719/726不属于可恢复集合；16条恢复必须完整清单和源身份一致。
- 方案通过；发布仍需测试、GitHub推送和备份门禁。
