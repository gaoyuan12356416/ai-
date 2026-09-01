# 测试报告

## 测试结论

exact release `7dd8b76ae29c8d74c55997f12601929a855e3959` 已完成本地回归、香港 T4
真机验收和生产发布。正式任务 `679e7c49acbf4af79f78bf60d76c5dd7` 已自然续制完成，
CPU 状态为 `done / 100% / 全部产物已生成`；首、中、尾抽帧均为单路 9:16 full-bleed
成片，不含左右双画面、中央接缝或封面占半屏。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 计划验收用例 | 13 | 13 | 0 | 0 |
| full-bleed profile 定向回归 | 179 | 179 | 0 | 0 |
| 完整自动回归 | 507 | 507 | 0 | 0（另 6 项按既有平台条件跳过） |

## 缺陷情况

累计记录 5 项：诊断上下文兼容问题见 `bugs/BUG-001.md`；`bugs/BUG-002.md` 是已被真机证伪的时间轴诊断假设；`bugs/BUG-003.md` 保留 legacy 几何根因证据；移除错误画布见 `bugs/BUG-004.md`；横屏 contain 仍形成横向分层及 full-bleed 修复见 `bugs/BUG-005.md`。

## 验证证据

本地命令：

```text
python -m unittest scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_upgrade
Ran 507 tests in 31.939s - OK (skipped=6)
```

full-bleed profile 定向命令：

```text
python -m unittest scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_upgrade
Ran 179 tests in 11.396s - OK
```

已把 `drama-legacy-intro-resume-20260901` 的旧片头隔离、严格任务目录和恢复错误语义合入 V2。exact clean commit、完整回归、最终真机报告和正式任务续制均已完成。

候选 `d2b49b2` 在香港 T4 的 exact release preflight 与 root 离线 503 项回归通过；服务用户直接运行完整测试时有 3 项被旧验收证据目录的既有权限拒绝，未修改旧证据权限。该候选的真实 30 秒视觉对照两条视频均完整解码，但 SSIM 为 `0.865390 < 0.90`，已拒绝且未切换生产。候选 `782c41d` 的 source PTS 假设复测为 `0.862533`，同样拒绝。进一步隔离确认 legacy 的错误 `rotw(iw)/roth(ih)` 画布和 YUV420 偶数坐标才是根因；兼容候选 `d7e121f` 达到 `0.931398`，但因会保留横向断层与异常裁切，被用户明确拒绝作为最终成片。当前 clean profile 不再以 legacy SSIM 为发布基线。

clean centered 候选 `dfb22cc` 的竖屏 30 秒样本完整解码，正确角度参考与 GPU 候选
SSIM 为 `0.930477`，三张单画面预览无断层。后续 exact 候选 `08985e6` 的横屏样本
完整解码但 SSIM 为 `0.899375`，更重要的是人工抽帧仍显示 contain 主体的上下横向
边界，因此按业务视觉直接拒绝；未以接近阈值为由放行。最终 v4 full-bleed 复测如下。

### v4 full-bleed exact 候选证据

- exact release：`7dd8b76ae29c8d74c55997f12601929a855e3959`，香港 T4 干净 detached checkout；
  真机完整回归 `Ran 507 tests in 35.575s - OK`。
- 真实横屏 30 秒：CPU clean full-bleed 参考与 GPU 候选均完整解码，SSIM
  `0.909267`；人工检查三张候选单画面，无中间矩形、上下横带、左右拼接或对比图。
- 正式任务同源竖屏 30 秒：两条视频均完整解码，SSIM `0.929829`；人工检查三张
  候选单画面，主体、字幕和模板元素位置正常。
- 正式任务同源 300 秒、120 秒分片、2 lanes：输出合同通过，耗时 `193.005s`，
  `1.554x realtime`，显存峰值 `1239 MiB`，子进程 RSS 峰值 `599500 KiB`，
  swap 增量 `0`，输出 SHA-256
  `56b57d362a97fb2b76f7fcc3f783f5b4b9e14c583e454cd0713daa8ecf329297`。
- 120 秒与 240 秒边界前后各三帧人工检查连续；主体与模板无复位、跳帧或接缝。
- GPU 健康信息已读回 backend=`opencl_fused_v2`、profile=
  `drama-opencl-fused-h264-720x1280-v4-fullbleed`、lanes=`2`、release 为上述 exact SHA；
  CPU 反向隧道读回同一健康身份。
- 生产 GPU `current` 精确指向上述 release；GPU worker、反向隧道、CPU API 和 CPU
  job worker 均为 `active`，`NRestarts=0`，当前运行中制作租约为 `0`。
- 正式任务 generation `4` 完成 `47/47` 个分片；最终视频 `5574.033333s`、
  `4145676743` bytes，SHA-256
  `a3d588996cf71fa2fe242f44b1d4a99957bff1f8b735db073c48f578ad790e00`；
  H.264 High 720×1280 30fps + AAC LC 48kHz stereo，完整硬件解码通过。
- GPU v3 result manifest 已绑定 object key、ETag、SHA-256、size 和上传 binding；COS
  HEAD 返回 `200 video/mp4`，Content-Length 与本地一致，ETag 为
  `"d0db79f0f0a3039f3e58bf35148d8014-248"`。
- CPU SQLite `quick_check=ok`；任务 attempt `8` 已以 `done` 释放，无错误，完成通知时间为
  `2026-09-01 21:13:14 CST`；CPU 持久快照与 GPU generation `4`、URL、SHA 和配方绑定一致。
- 部署前 CPU/GPU 配置、代码、SQLite 与旧 release 均已保留在带时间戳的备份目录；
  未核验的旧局部成片已移入 GPU 备份区而非删除，恢复沿正式 generation/lease 状态机执行。

## 遗留风险

本次发布无阻塞项。随机模板会按冻结配方更换装饰素材，但主体始终使用同一条 full-bleed
剧集画面；具体装饰图案不是固定视觉合同。持续运行仍依赖 GPU 数据盘、反向隧道和 COS，
任一身份或产物校验不一致时保持 fail closed，不回退到旧 CPU overlay graph。

## T4 阶段实测

| 候选 | 样本 | 配置 | 结果 | 吞吐 | 显存峰值 | swap 增量 |
|---|---:|---|---|---:|---:|---:|
| `0d8d04f` | 30 秒 | 1 lane | 输出契约通过 | 1.104× | 663 MiB | 0 |
| `0d8d04f` | 300 秒 | 2 lane | 输出契约通过 | 1.357× | 1395 MiB | 0 |
| `4cbd8fe` | 300 秒 | 上限 4（实际 3 块并行）、每输入 decoder/complex filter 各 2 线程 | 输出契约通过 | 1.300× | 1883 MiB | 0 |
| `4cbd8fe` | 300 秒 | 2 lane、每输入 decoder/complex filter 各 2 线程 | 输出契约通过 | **1.388×** | 1263 MiB | 0 |
| `7dd8b76` | 300 秒正式同源 | v4 full-bleed、120 秒分片、2 lane、每输入 2 线程 | 输出契约与视觉通过 | **1.554×** | 1239 MiB | 0 |

因此生产默认选 `2 lane`，每输入 decoder 与 complex filter 各限制 2 线程；保留 1～4 lane 泛化能力，但不把最大并发误当成最高吞吐。最终 exact release 的视觉对照、受控恢复、92.9 分钟正式长任务和生产切换均已完成；正式任务没有通过数据库强改状态或未核验局部文件绕过验收。

## 发布建议

`PASS`：维持 `7dd8b76`、v4 full-bleed、120 秒分片和 2 lanes 的生产配置。若后续健康身份、数据盘或 COS 产物绑定失配，按已保留检查点回滚，禁止静默降级。
