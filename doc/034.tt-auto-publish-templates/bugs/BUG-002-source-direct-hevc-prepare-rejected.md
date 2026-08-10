# BUG-002: 自动发布冻结 HEVC 素材后 prepare 被拒绝

## 影响

生产运行 10 的任务 34（账号 640、素材 `6028067`）已完成选材、素材永久预留、文案和四位码冻结，但在共享 GPU prepare 阶段进入 `retry_wait`。没有 prepared URL、publish ID 或未知发布结果，未调用 TikTok。

## 根因

自动发布 CPU 与 GPU 均正确使用 `tt-post-source-direct-v1` 和零裁尾。失败来自共享 GPU 的 source-direct 原片合同只允许 H.264/`avc1`，而该素材为符合其他全部边界的 HEVC Main/`hvc1`。

## 修复边界

修复位于共享 `features/tt_gpu/worker.py`：source-direct 继续保持原字节镜像，同时允许严格配对的 H.264/`avc1` 与 HEVC/`hvc1`。自动发布选择、素材 ledger、任务身份、账号凭据、TikTok publish 和生产闸门均不修改。

任务 34 保留原 `gpu_job_id`、素材、文案和四位码，通过部署后的自然重试恢复；不得创建替代任务或重新选材。

## 生产结果

2026-08-07 19:17:44 CST，任务 34 使用原 `gpu_job_id=ttauto-34-63514ecd60116a7ca0c7bc290a3ac4edf297` 完成 prepare；19:18:08 取得一次 `publish_id=v_pub_url~v2-1.7671247289059182610`，19:19:04 经 reconcile 收敛为 `published`。最终 `publish_attempt_count=1`、`unknown_outcome=0`，没有创建替代任务。
