# 开发计划

## 开发范围

新增 GPU `source_direct` 模式、profile 与 manifest 合同；CPU 沿用现有 profile 握手和数据库字段，仅增加配置示例。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 新增 mode/profile/config 校验 | `features/tt_gpu/worker.py` | 已完成 |
| 原片下载、ffprobe、SHA、原字节镜像 | `features/tt_gpu/worker.py` | 已完成 |
| manifest v6 与复用/发布再校验 | `features/tt_gpu/worker.py` | 已完成 |
| 自动化测试 | `scripts/test_tt_gpu_worker.py` | 已完成 |
| 配置、部署、回滚文档 | `deploy/*.env.example`、本目录 | 进行中 |
| GitHub-first CPU/GPU 部署 | 生产两台服务器 | 待执行 |

## 编译与验证命令

```powershell
$env:PYTHONPYCACHEPREFIX = Join-Path $env:TEMP 'codex-tt-source-direct-pycache'
python -m py_compile features\tt_gpu\worker.py scripts\test_tt_gpu_worker.py
python -B -m unittest scripts.test_tt_gpu_worker -v
python -B -m unittest scripts.test_tt_posts_core scripts.test_tt_posts_service scripts.test_tt_post_prepare_runner scripts.test_tt_post_direct_config_core scripts.test_tt_posts_app_contract -v
git diff --check
```

## 风险与依赖

- 依赖当前 COS 镜像存储和 `socialkit-cdn.yingliang.tech` URL Property 验证保持有效。
- CPU/GPU 必须部署同一 GitHub commit，CPU profile 与 GPU mode 必须成对切换。
- 部署时暂停 runner/prepare timer 与 path，确认无运行中 oneshot 后再切换，完成后恢复原状态。

## 完成记录

- 2026-08-07：定向 source_direct 测试与全部 TT Python 403/403 回归通过，其中 GPU 70/70。
