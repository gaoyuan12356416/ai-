# 部署文档

## 变更内容

GPU release 增加 Composition/renderer 模块和 OpenCL kernel；CPU 仅需同一 exact commit 的兼容代码与 V2 backend 无关。生产配置切换 GPU backend、chunk 和 lane。

## 配置项

- `DRAMA_GPU_COMPOSITOR_BACKEND=opencl_fused_v2`
- `DRAMA_GPU_CHUNK_SECONDS=120`
- `DRAMA_GPU_CHUNK_TIMEOUT=1800`
- `DRAMA_GPU_OPENCL_DEVICE=0.0`
- `DRAMA_GPU_RUNTIME_IDENTITY=<已核验的 FFmpeg/OpenCL/NVENC 运行时版本>`
- `DRAMA_GPU_MAX_CONCURRENCY=1`（完整任务固定单并发）
- `DRAMA_GPU_COMPOSITOR_LANES=2`（同一任务内部双分片）

## 数据库变更

无。分片与 Composition 检查点位于 `/data/drama-synthesis-gpu/work/compositor-cache`，任务运行目录仍为 `/data/drama-synthesis-gpu/work/jobs`。

## 部署步骤

1. 推送并记录 exact GitHub commit。
2. 核对 CPU worker、GPU runtime、数据盘及无在途正式制作。
3. 分别备份 CPU drama 文件/单位/配置和 GPU current release/单位/配置；配置备份不进入 GitHub。
4. 从 GitHub exact commit 构建 `/data/drama-synthesis-gpu/releases/<40位commit>` immutable release；preflight 和 benchmark 都必须解析到该目录。
5. 切换 GPU `current`，重启 `drama-synthesis-gpu-worker.service`，保留 tunnel。
6. 先在不切换服务的候选 release 上运行真实五输入 preflight、短样、受控中断/同缓存续跑和约 79.4 分钟长样；完整任务保持 1，并验证 2 条分片 lane 同属一个任务。
7. CPU 同步 exact commit 的隔离文件并重启 API/worker，验证异步提交和轮询，不创建正式重试。

## 验证步骤

- `/healthz` renderer/backend/profile 与配置一致。
- OpenCL/NVENC 真机 5 秒及长样 benchmark。
- systemd status/journal 无 kernel、OOM、swap 或 checkpoint 错误。
- CPU `/api/auth/status`、GPU tunnel 和异步只读状态正常。

## 回滚方案

1. 停止 CPU intake，等待/保留当前分片状态。
2. GPU `current` 切回部署当时已备份并核验的真实旧 release target，恢复原 worker env/unit，重启 GPU worker。
3. CPU 恢复部署前隔离文件和配置，重启 API/worker。
4. 不恢复旧 SQLite、不删除 V2 分片；旧 renderer 会忽略 V2 私有检查点。

## 注意事项

不把延长总超时作为 V2 修复；不覆盖服务器其他功能文件；不自动重试两个历史失败任务。
