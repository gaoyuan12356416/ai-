# 测试报告

## 测试结论

本地候选自动回归通过，可进入 exact commit 的 T4 隔离验收；尚未形成生产发布结论。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 计划验收用例 | 12 | 8 | 0 | 4 |
| 自动回归 | 500 | 500 | 0 | 0（另 6 项按既有平台条件跳过） |

## 缺陷情况

开发期发现 1 个诊断上下文兼容缺陷，已修复并回归，见 `bugs/BUG-001.md`。

## 验证证据

本地命令：

```text
python -m unittest scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_upgrade
Ran 500 tests in 28.369s - OK (skipped=6)
```

exact commit 与真机报告待候选提交后补充。

## 遗留风险

## T4 阶段实测

| 候选 | 样本 | 配置 | 结果 | 吞吐 | 显存峰值 | swap 增量 |
|---|---:|---|---|---:|---:|---:|
| `0d8d04f` | 30 秒 | 1 lane | 输出契约通过 | 1.104× | 663 MiB | 0 |
| `0d8d04f` | 300 秒 | 2 lane | 输出契约通过 | 1.357× | 1395 MiB | 0 |
| `4cbd8fe` | 300 秒 | 上限 4（实际 3 块并行）、每输入 decoder/complex filter 各 2 线程 | 输出契约通过 | 1.300× | 1883 MiB | 0 |
| `4cbd8fe` | 300 秒 | 2 lane、每输入 decoder/complex filter 各 2 线程 | 输出契约通过 | **1.388×** | 1263 MiB | 0 |

因此生产默认选 `2 lane`，每输入 decoder 与 complex filter 各限制 2 线程；保留 1～4 lane 泛化能力，但不把最大并发误当成最高吞吐。最终 exact release 的视觉对照、受控恢复、79.4 分钟长样与生产切换，因同机另一已授权迁移窗口正在执行而暂停，未绕过验收。

## 发布建议

允许提交候选并运行隔离真机验收；暂不切换生产 worker。
