# 部署、生产验证与回滚

## 已部署范围

- 时间：2026-09-15，北京时间；20:01:58 已确认正式服务与隧道健康。
- GitHub 分支：`codex/drama-gpu-frame-pipeline-20260915`，代码提交 `a93b541335ca4ba0183a8cc6ad7b46a7eb31233d`。
- HK：`43.154.250.89:/data/drama-synthesis-gpu/current` → `releases/a93b541335ca4ba0183a8cc6ad7b46a7eb31233d`。
- 仅启用 `drama-synthesis-gpu-worker.service` 新帧链路，恢复 `drama-synthesis-gpu-tunnel.service`。CPU 代码、API/observer、TT/FB worker 未切换；无数据库迁移。
- 新配置：`/etc/systemd/system/drama-synthesis-gpu-worker.service.d/99-z-native-frame-pipeline.conf`。worker.env 保持原值；下载 6、预取 2、下载阶段允许预取、两分片并行均已从新主进程环境确认。

## 备份与切换

回滚备份：`/data/drama-synthesis-gpu/backups/20260915-frame-native-a93b541`。包含旧 symlink/release、worker.env、systemd 单元/drop-in、私有 runtime JSON、文件前缀清单和切换验证。

先本地提交并推送 GitHub，再在 HK 创建精确 SHA 的 detached release；安装独立 SHA 锁定依赖，完整校验 V2 缓存；真实服务 UID/GID 预检通过后切换。

旧任务 `e4e4a464451a4480a985b23e749fe9b4` 在规格化阶段、无跟踪子进程/启动意图及额外 OS 子进程时请求停止服务。SIGTERM 后 Python 仍等待最后一个下载线程；对准确旧 MainPID 先 SIGSTOP 冻结，复核 cgroup 仅该进程、仍处于下载/规格化、无子进程和启动意图，保存稳定文件前缀，然后只终止该旧进程。未对活动视频处理子进程执行强杀。

确认 MainPID=0 后写入新增 drop-in、原子切换 current、daemon-reload、启动 worker 和 tunnel。未恢复旧数据库或旧 runtime JSON。

## 实际验证

- 实际 systemd 沙箱 ExecStartPre：ok=true、issues=[]、cuda_tested=true、compositor_pipeline_checked=true、asset_cache_verified_count=18、app_import_checked=true。
- worker MainPID 4106900、tunnel MainPID 4106901，均 active / NRestarts=0。
- GPU 8787/healthz 与 CPU 18788/healthz 返回 200，frame_pipeline=cuda、asset_cache_enabled=true，release_sha 与运行目录一致。
- CPU API topbar 200；drama-material-api 与 drama-material-job-worker 均 active、NRestarts=0。
- 原 job_id 自动恢复为 generation=2；CPU remote_runtime 同步 generation=2，observer lease 仍原 attempt=1、last_error 为空。
- 107 个停止时的素材文件前缀全部匹配，无缺失、无改写。恢复时先验证磁盘素材，进度计数暂时重算；随后确认 53/54 下载与规格化文件复用，最后一集从已有 partial 继续传输。
- 20:04:47 CPU 回读：任务 98%，已下载 53/54、规格化 53/54、4750.3MB/4756.5MB、1.02MB/s，无错误。
- 20:07:58 GPU 回读：最后一集已续传并完成规格化，合并视频完成，原任务进入上传（1,862,270,976 / 3,418,532,325 字节）；两服务仍 active、NRestarts=0。
- 剩余业务任务只启用了 concat_video，random_template_video=false。原生模板渲染在真实服务预检和私有完整样片中验证；该拼接任务本身不会触发模板 CUDA 渲染。新随机模板任务自动使用新链路。

证据保存在备份目录；固定样片见 test-report.md。本地 `artifacts/drama-gpu-frame-pipeline-20260915/evidence-summary.json` 为脱敏汇总，不含 worker.env、原始业务 payload 或凭证。

## 回滚步骤

1. 选择无制作子进程的维护边界，停止接单并确认原 worker 及其子进程退出。不能仅凭心跳超时删除检查点或杀 PID；先验证 PID > 1、boot/start_ticks、所属 cgroup 和任务阶段。
2. 停止 `drama-synthesis-gpu-worker.service`；确认 MainPID=0 和对应 cgroup 没有遗留进程。
3. 核对旧 release `/data/drama-synthesis-gpu/releases/6e37c72b9a730e190a43a3ee41a21f5b4b5d1866` 的 Git HEAD。将本次唯一新增的 `99-z-native-frame-pipeline.conf` 移入上述备份目录保存，恢复其余原 drop-in 的配置效果；worker.env 无需改动。
4. 将 `/data/drama-synthesis-gpu/current` 原子切回该旧 release，运行 `systemctl daemon-reload`，启动 worker 和 tunnel。
5. GPU 验证 8787/healthz，CPU 验证 18788/healthz；核对原任务 generation/lease 和结果。保留下载、分片、结果、V2 缓存与现有账本，禁止将旧 runtime JSON 或数据库覆盖回生产。

## 技能更新

已备份并更新 ai-backend-maintenance 的 `references/ad-material-current-context.md`，新增 `drama-native-gpu-frame-pipeline-20260915`。记录代码/依赖/缓存、时间轴限制、固定样片测量和回滚；未修改 memory 文件。
