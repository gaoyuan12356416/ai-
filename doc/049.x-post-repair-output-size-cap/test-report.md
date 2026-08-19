# 测试报告

## 自动化覆盖

- 码率预算：短视频保持既有参数；900 秒与四小时边界动态降码率并保留体积余量。
- FFmpeg 参数：NVENC、CFR 30、GOP 60、AAC-LC、画布与时长策略不变。
- 清单：新 profile 和码率决策写入不可变修复清单。
- 失败关闭：空文件、超限文件、时长/编码/尺寸不一致仍在 COS/X 写入前拒绝。

## 生产验收边界

- 使用 GPU 真实生成的离线样本验证，不创建 X Post。
- 以健康、profile、文件 SHA/大小/probe、SQLite `quick_check`、外键、未知结果和自然 timer 作为上线证据。

## 结果

- 本地专项测试：108 通过、1 个条件跳过；完整 X 回归：694 通过、2 个条件跳过；`py_compile`、前端快速语法检查和 `git diff --check` 通过。
- GPU 服务专项测试：19/19 通过；离线真实 NVENC 样本使用 2 MiB 上限生成 20.01 秒文件，成品 1,738,249 bytes，SHA-256 `4e409b67363532731b59112ba83d3f42949d6e1098151a3052788bc21442cda8`。动态视频码率为 491 kbps，低于上限并保留 12% 预算余量。
- GPU 健康接口返回 profile `x-h264-nvenc-720-duration-policy-v5`；CPU 日常调度和 X Auto 进程环境均加载同一 v5 profile。
- CPU release 精确提交为 `d87744902f2b8d06dff982df3dc0eeeb8d9ebcd8`；X OAuth 与 X Auto 健康通过，五个 timer 均为 `active/enabled`，上线后 journal 无 warning/error。
- 主发布库 `quick_check=ok`、外键违规 0；queue/log 均为 412 条，其中 411 published、1 failed，active/unknown 均为 0。X Auto 库 `quick_check=ok`、外键违规 0。
- 没有补发 2026-08-18 的失败批次，没有执行 run-now、手动发布或真实测试 Post；自然 timer 只得到 claim 0、manual `no_pending` 和无待执行 X Auto 任务。
- Drama 配置由 version 17 更新到 version 18，仅移除没有同语言剧集来源的日语账号 19、20；2026-08-20 冻结计划为 17 个英语账号，既有 2026-08-19 version 17 计划未改写。
