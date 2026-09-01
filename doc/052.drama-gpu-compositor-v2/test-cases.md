# 测试用例

## 测试范围

Composition 严格校验、scene hash、帧分片、kernel 安全生成、FFmpeg 命令、检查点恢复、音频 mux、backend 禁止降级、并发配置、现有异步/缓存回归和 T4 性能。

## 测试数据

- 合成 5 秒 16:9/9:16 H.264+AAC fixture。
- 生产同源小样但不上传/不创建业务任务。
- 两个历史失败任务的源时长和随机配方仅用于离线基准，不自动重试任务。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | Composition 规范化 | 合法随机配方 | 两次编译 | JSON 与 SHA 完全一致 | P0 | 自动通过 |
| TC-002 | 非法 scene | 未知字段/越界变换 | 编译 | 启动 FFmpeg 前显式拒绝 | P0 | 自动通过 |
| TC-003 | 分片边界 | 5400.1 秒、30fps | 默认规划 | 无空片/重叠/缺帧，最后一片正确 | P0 | 自动通过 |
| TC-004 | fused kernel | 五图层 | 编译 kernel | 仅安全常量，包含旋转/scale/tint/alpha | P0 | 自动通过 |
| TC-005 | 命令合同 | OpenCL backend | 构建 chunk 命令 | 单次 `program_opencl`、NVENC、无 legacy overlay graph | P0 | 自动通过 |
| TC-006 | 分片恢复 | 第 2 片失败 | 重启执行 | 第 1 片不重制，第 2 片继续 | P0 | 自动通过，真机待验 |
| TC-007 | 身份冲突 | 修改源或 scene | 重放 | fail closed，不覆盖旧片 | P0 | 自动通过 |
| TC-008 | 连续音频 | 多分片、有/无音轨 | 合并和 mux | 单音轨、无接缝、时长误差≤0.15秒 | P1 | 自动通过，真机待验 |
| TC-009 | backend 禁止降级 | V2 不可用 | 执行 | 返回 compositor unavailable，不启动 CPU graph | P0 | 自动通过 |
| TC-010 | lane 配置 | 全任务1、分片1～4/非法值 | worker 启动 | V2 全任务仅1；分片1～4生效，其他 fail closed | P1 | 自动通过 |
| TC-011 | T4 小样视觉 | 真实素材及配方 | CPU/V2 双渲染抽帧 | 构图、素材、旋转和时长符合合同 | P0 | 待执行 |
| TC-012 | T4 长样性能 | ≥30分钟源 | V2 基准 | ≥1.5x realtime、无 swap、资源有界 | P0 | 待执行 |

## 回归范围

现有 drama media pipeline、GPU runtime/cache、remote client、CPU catalog、upgrade contracts 及 app import。
