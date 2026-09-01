# SA 代码评审

## 结论

两轮独立代码复审未发现剩余 P0 代码缺陷。runtime fingerprint preflight 与检查点路径问题已修复；允许冻结 immutable candidate，生产切换仍取决于 T4 真机用例。

## 评审范围

Composition、OpenCL kernel/command、分片检查点、合并/mux、backend 路由、worker lane、配置与测试。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-001 | P0 | 分片/合并/mux subprocess | 进程状态不确定时删除临时证据，join/mux 未接入进程账本 | 全部子进程使用统一跟踪；不确定时保留，确认停止的新 generation 才清理 | 已修复 |
| CR-002 | P1 | 分片媒体合同 | 仅时长/分辨率不足以保证 stream-copy concat | 校验精确帧数、首包关键帧、SPS/PPS SHA、timebase、色彩与音频合同 | 已修复 |
| CR-003 | P1 | cache identity | 未绑定分片规划与实际运行时 | 纳入 plan、release、FFmpeg 指纹、OpenCL device、GPU/driver hash | 已修复 |
| CR-004 | P1 | preflight | 服务启动未执行真实五输入与 runtime fingerprint | 启动前执行 1 秒 program_opencl→NVENC，并校验受信 nvidia-smi/运行时指纹 | 已修复 |
| CR-005 | P1 | 并发 | 两个完整任务会争抢 Demucs、显存和收尾缓存 | V2 固定完整任务 1；仅同一任务内部允许 1～4 个 chunk lane | 已修复；T4 对照选择 2 lane × 2 threads |
| CR-006 | P2 | 部署文档 | compositor cache 路径写成 jobs | 改为 `work/compositor-cache` | 已修复 |

## 编译 / 验证结果

本地 Python 3.9 AST 校验通过；500 项相关完整回归通过，6 项按既有平台条件跳过。真机结果记录到测试报告。
