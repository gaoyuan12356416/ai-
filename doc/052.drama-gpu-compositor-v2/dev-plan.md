# 开发计划

## 开发范围

实现 Composition Spec v1、OpenCL fused renderer、分片检查点/合并/音频 mux、backend 路由、lane 配置和部署资料。

## 任务拆分

| 任务 | 负责人 | 文件/模块 | 状态 |
| --- | --- | --- | --- |
| 场景协议与分片计划 | Codex | `features/drama_synthesis/composition.py` | 已完成 |
| GPU shader 与命令编译 | Codex | `features/drama_synthesis/gpu_compositor.py`, `features/drama_synthesis/opencl/*` | 已完成 |
| legacy 兼容与全局进度 | Codex | `features/drama_synthesis/gpu.py` | 已完成 |
| worker lane/健康能力 | Codex | `scripts/drama_synthesis_gpu_worker.py` | 已完成 |
| 自动测试与真机基准 | Codex | `scripts/test_drama_gpu_compositor_v2.py`, benchmark/compare | 自动测试完成，真机待执行 |
| GitHub/CPU/GPU 发布 | Codex | deploy/doc | 候选待提交与隔离验收 |

## 编译 / 构建命令

```bash
python -m py_compile features/drama_synthesis/composition.py features/drama_synthesis/gpu_compositor.py features/drama_synthesis/gpu.py scripts/drama_synthesis_gpu_worker.py
python -m unittest scripts.test_drama_gpu_compositor_v2
python -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client
git diff --check
```

## 风险与依赖

- 香港 T4 的 OpenCL ICD、`program_opencl`、libvpx-vp9 alpha 解码和 NVENC 必须共同可用。
- 分片 MP4 的 SPS/PPS SHA、首包关键帧、timebase 和编码参数必须一致，最终 concat 不允许重编码视频。
- 生产发布前确认无在途 drama worker；部署时读取、备份并核验真实 current release，不依赖文档硬编码。

## 完成记录

2026-09-01：本地 500 项回归通过、6 项按既有平台条件跳过；等待 exact commit 后的 T4 隔离验证。
