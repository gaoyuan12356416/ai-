# 开发与交付计划

更新：2026-08-28。工作分支 `codex/drama-synthesis-reliability-20260828`，源码基线 `420957be4c288308c38b97f773be330208887204`。本计划记录已落地代码和剩余门禁，不能视为发布完成证明。

## 任务拆分与交付顺序

| 工作包 | 责任 | 主要交付 | 当前状态 |
| --- | --- | --- | --- |
| 输入与生产边界确认 | 主任务 | accepted-design、CPU/HK版本与正在制作任务核对 | 设计已记录；切换前仍须重新核对 |
| GPU持久异步运行时 | GPU执行子任务 | async_runtime、GPU HTTP提交/查询/resume、进程/代次栅栏与测试 | 代码已实现；本地通过，Linux待验 |
| CPU连接与状态事务 | CPU执行子任务＋主任务整合 | remote_client、cpu_runtime、worker租约保护、配方/完成事务 | 代码已实现；本地通过，业务链路待隔离验证 |
| 专用媒体流水 | 媒体执行子任务＋主任务整合 | 安全下载、单标准化执行器、模板检查点、FFmpeg进度 | 代码已实现；合成及故障单测通过，长片待验 |
| 任务列表和操作员展示 | 主任务 | app_support、drama-job-runtime、两个现有页面 | 已接入并通过静态回归；浏览器视觉待验 |
| 下载与渲染对照工具 | 媒体执行子任务 | benchmark_drama_synthesis_media及私有证据目录 | 工具已实现；CDN样本结果分化、默认不变；下载并发/CPU组合待实测决定 |
| 评审、回归和发布 | 主任务 | 七份合同文档、代码评审、测试报告、部署与回滚证据 | 文档补齐；最终回归和生产切换未完成 |

实施顺序：完成代码整合和审查 → 本地核心/旧功能回归 → 隔离Linux故障与接口验收 → 固定短样和约90分钟长片 → 性能/候选域名决策 → 原任务自然结束并对账 → GitHub精确版本发布 → GPU兼容接口 → CPU开关 → 人工验收。

## 本地构建与回归

以下命令在仓库根执行，不需要真实业务Token、COS写入或GPU推理。Python版本与目标GPU运行时兼容，不使用Maven。

```text
python -m py_compile app.py features/drama_synthesis/async_runtime.py features/drama_synthesis/cpu_runtime.py features/drama_synthesis/remote_client.py features/drama_synthesis/app_support.py features/drama_synthesis/media_pipeline.py features/drama_synthesis/local_checkpoint.py features/drama_synthesis/gpu.py scripts/drama_synthesis_gpu_worker.py scripts/drama_job_worker.py scripts/benchmark_drama_synthesis_media.py
python -m unittest scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_media_pipeline
python -m unittest scripts.test_drama_synthesis_upgrade scripts.test_drama_synthesis_cpu_catalog
node --check static/drama-job-runtime.js
node scripts/test_drama_synthesis_list_actions.js
git diff --check
```

记录命令、代码版本、时间、退出码和数量；每次代码修订后重跑受影响的检查。核心四套的本次执行为 **162项通过，9.921秒**（GPU runtime62、cache22、CPU客户端30、media48）；静态列表回归为 **16项通过、2页、0次浏览器调用、0次网络调用**。上述其他命令及完整回归的最终记录由 [test-report.md](test-report.md) 汇总，不在本计划预填通过。

## 验收工具与数据纪律

- `benchmark_drama_synthesis_media.py` 必须显式带 `--apply`，要求新的绝对输出目录；不提交生产任务、不上传COS、不发布任何平台内容。
- 下载样本URL通过0600私有JSON文件传入，日志/证据只记录哈希、源主机、长度和样本校验。单次最多256 MiB，单资源最多32 MiB。
- 8路测试必须提供相同样本的成功4路基线。域名比较使用同并发与同资源样本，不把比较标签当作缓存身份。
- 渲染短样实际时长0.5～300秒；长样5400～7200秒。相同源文件SHA、冻结配方、素材manifest及编码参数，使用独立输出目录，证明启动了真实渲染器而非命中检查点。
- 2核/2线程、4核/2线程、4核/4线程由隔离systemd执行范围提供CPU配额；不得改正式服务来做对照。记录RSS、线程、CPU节流、GPU占用和耗时；长片还要完整解码、音画与模板视觉验收。

## 发布依赖与停止条件

保留现网后续修复；先推送经审查的精确GitHub提交，部署时记录实际SHA。原任务尚在制作、源/配方/检查点冲突、进程状态不明、磁盘不足、资源影响同机服务或长片效果不合格时均停止切换。

`DRAMA_GPU_ASYNC_ENABLED` 默认OFF；候选域名、下载8路和CPU配额提升各自独立评估，任何一项没有证据就维持原配置。生产切换和回滚按 [deploy.md](deploy.md)；默认配置不能被“工具运行成功”自动修改。

## 完成记录

2026-08-28：本地实现与核心回归完成到上述范围；文档记录已补齐。Linux故障、性能、长片、生产切换及人工验收仍待完成。本轮文档子任务未操作远端、未提交代码。
