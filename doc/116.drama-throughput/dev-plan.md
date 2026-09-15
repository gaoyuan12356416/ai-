# 实施计划

1. 基于 HK 生产 df1c495545a78d25d313a5b3443d02a8fb46a759 独立开发，核对 CPU 文件 SHA。
2. 固定 8 个来源对比两线路与 4/6/8；初测每条最多 8 MiB，复测每条最多 32 MiB，单轮不超过 256 MiB。
3. 修改领取顺序和观察线程，新增 GPU 预取与阶段显示；线上确认下载空余后，开启总并发上限 8 的下载阶段重叠。
4. 运行离线测试、编译和 diff 检查，推送 GitHub，两端从精确提交复测。
5. 备份、部署、核验自然任务、记录回滚点。

验证命令：

```text
python -m unittest scripts.test_drama_throughput scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline -q
python -m py_compile features/drama_synthesis/prefetch.py features/drama_synthesis/async_runtime.py features/drama_synthesis/app_support.py scripts/drama_job_worker.py scripts/drama_synthesis_gpu_worker.py
git diff --check
```
