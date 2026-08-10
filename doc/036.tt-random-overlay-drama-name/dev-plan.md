# 开发计划

## 开发范围

共享文案渲染、两套 TT 调用链、GPU 新制作模式、资产构建器、前端提示、部署配置、测试和文档。

## 任务拆分

| 任务 | 文件/模块 | 状态 |
| --- | --- | --- |
| 剧名宏契约与调用链 | `features/tt_posts/`, `features/tt_auto_posts/`, TT 页面 | 进行中 |
| 资产清单/配方/命令构建 | `features/tt_gpu/`, `scripts/` | 进行中 |
| GPU worker 集成和 manifest 校验 | `features/tt_gpu/worker.py` | 待完成 |
| 环境示例与部署脚本 | `deploy/` | 待完成 |
| 单元、回归、离线合成 | `scripts/test_tt*.py` | 待完成 |

## 构建与测试命令

```powershell
python -B -m py_compile features/tt_posts/core.py features/tt_posts/service.py features/tt_auto_posts/links.py features/tt_auto_posts/publisher.py features/tt_gpu/worker.py
python -B -m unittest discover -s scripts -p "test_tt*.py" -v
git diff --check
```

## 风险与依赖

- GPU `/data` 必须保持真实挂载且空间充足。
- 透明 WebM 解码依赖生产 FFmpeg `libvpx-vp9`；已只读确认存在。
- 自动发布门禁当前开启，切换时必须暂停 scheduler/runner，确认无 in-flight 后再换 release/profile。

## 完成记录

- 2026-08-10：已合并线上 CPU auto `3c3db5a` 与 GPU `7e428f5` 的维护谱系；黑底透明原型通过。
