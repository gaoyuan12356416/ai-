# 测试报告

## 测试结论

本地候选自动回归通过，可进入 exact commit 的 T4 隔离验收；尚未形成生产发布结论。

## 测试范围

见 `test-cases.md`。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 计划验收用例 | 13 | 9 | 0 | 4 |
| clean profile 定向回归 | 178 | 178 | 0 | 0 |
| 完整自动回归 | 506 | 506 | 0 | 0（另 6 项按既有平台条件跳过） |

## 缺陷情况

累计记录 4 项：诊断上下文兼容问题见 `bugs/BUG-001.md`；`bugs/BUG-002.md` 是已被真机证伪的时间轴诊断假设；`bugs/BUG-003.md` 保留 legacy 几何根因证据；用户拒绝兼容缺陷后的 clean profile 修复见 `bugs/BUG-004.md`。

## 验证证据

本地命令：

```text
python -m unittest scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_media_pipeline scripts.test_drama_synthesis_gpu_cache scripts.test_drama_synthesis_remote_client scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_upgrade
Ran 506 tests in 31.427s - OK (skipped=6)
```

clean profile 定向命令：

```text
python -m unittest scripts.test_drama_gpu_compositor_v2 scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_synthesis_upgrade
Ran 178 tests in 11.536s - OK
```

已把 `drama-legacy-intro-resume-20260901` 的旧片头隔离、严格任务目录和恢复错误语义合入 V2。exact clean commit、完整回归与最终真机报告待候选提交后补充。

候选 `d2b49b2` 在香港 T4 的 exact release preflight 与 root 离线 503 项回归通过；服务用户直接运行完整测试时有 3 项被旧验收证据目录的既有权限拒绝，未修改旧证据权限。该候选的真实 30 秒视觉对照两条视频均完整解码，但 SSIM 为 `0.865390 < 0.90`，已拒绝且未切换生产。候选 `782c41d` 的 source PTS 假设复测为 `0.862533`，同样拒绝。进一步隔离确认 legacy 的错误 `rotw(iw)/roth(ih)` 画布和 YUV420 偶数坐标才是根因；兼容候选 `d7e121f` 达到 `0.931398`，但因会保留横向断层与异常裁切，被用户明确拒绝作为最终成片。当前 clean profile 不再以 legacy SSIM 为发布基线。

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

允许提交 clean 候选并运行隔离真机验收；代表性单画面预览通过前不切换生产 worker。
