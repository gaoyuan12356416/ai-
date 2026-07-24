# 013.x-post-media-repair SA 测试用例评审

## 结论

通过。测试范围覆盖自动触发边界、GPU 转码、COS 完整性、CPU 二次复检、SQLite 幂等迁移、同日计划恢复、显式回填和零发布约束。

## 重点覆盖

| 编号 | 风险 | 覆盖方式 | 结论 |
| --- | --- | --- | --- |
| TR-001 | 非允许错误误触发 GPU | codec/dimensions 以外错误断言 worker 调用数为 0 | 已覆盖 |
| TR-002 | codec 首错掩盖隔行扫描或超高帧率 | worker 对 source scan、FPS、duration 做完整二次分类 | 已覆盖 |
| TR-003 | GPU 返回伪造 URL、指纹或 probe | 客户端严格核对 HTTPS、job/profile、SHA、大小及 probe | 已覆盖 |
| TR-004 | 转码截断内容 | 输出时长必须与源时长在小容差内一致 | 已覆盖 |
| TR-005 | 同日重入重新选材或重复修复 | 计划查询必须先于账号、素材池、MySQL 和 GPU | 已覆盖 |
| TR-006 | 回填误建队列或发布 | 回填测试中的 `create_plan` / `publish` 为禁止调用 | 已覆盖 |
| TR-007 | 九条显式回填被 daily 六条上限截断 | 回填保持每素材最多一次，但不套用自动批次上限 | 已覆盖 |
| TR-008 | 报告写失败篡改真实结果 | 保留已完成计数，单独返回 report 错误 | 已覆盖 |
| TR-009 | 旧库升级破坏历史发布 | 新列仅增量迁移，重复初始化幂等 | 已覆盖 |

## 生产验收

- GPU 实机 NVENC 参数和输出 probe。
- COS 上传、HEAD 元数据、公开 HTTPS 下载。
- 当前九条素材全部通过 CPU 正式 `probe_media`。
- 回填前后 queue/log/Post 计数不变。
- timer、sidecar、worker、隧道和 SQLite integrity 均正常。
