# BUG-001 自动发布未随 GPU 切换原片直发 profile

## 发现阶段

2026-08-07 生产原片直发测试。

## 现象

自动发布 run 6 的任务 7–12 在准备阶段失败，错误为
`prepare_profile_mismatch: requested media profile does not match the GPU worker`。

## 复现步骤

1. GPU 切换为 `source_direct` / `tt-post-source-direct-v1`。
2. 保持自动发布 `/etc/tt-auto-post.env` 为旧 `direct_outro` profile 和 `4.333333` 秒裁尾。
3. 手动执行自动发布模板。

## 期望结果

自动发布请求 `tt-post-source-direct-v1` 且裁尾为 `0`；GPU 只校验并镜像原始字节，不执行 FFmpeg 制作。

## 实际结果

自动发布仍请求旧 profile，GPU fail-closed；任务没有进入 TikTok publish，任务 7–12 均无
`publish_id`、`publish_attempt_count=0`、`unknown_outcome=0`。

## 根因分析

TT Post 素材池与 TT 自动发布是两个独立 CPU 服务。首次切换只更新了
`/etc/tt-post.env`，遗漏 `/etc/tt-auto-post.env` 的 profile 和裁尾配置；自动发布 health
又未暴露这两个值，因此部署健康检查没有发现消费者未对齐。

## 修复说明

- 自动发布生产配置成对切换到 `tt-post-source-direct-v1` 和裁尾 `0`。
- `source_direct` 搭配非零裁尾时，自动发布服务在启动阶段直接拒绝配置。
- `/health` 增加当前请求 profile 与裁尾秒数，部署时必须与 GPU health 对照。
- 旧 `direct_outro` 默认实现和回切值保留，不自动重试本次失败任务。

## 影响文件

- `features/tt_auto_posts/publisher.py`
- `features/tt_auto_posts/service.py`
- `deploy/tt-auto-post.env.example`
- `scripts/test_tt_auto_post_publisher.py`
- `scripts/test_tt_auto_post_service.py`

## 验证命令与结果

- `python -B -m unittest scripts.test_tt_auto_post_publisher scripts.test_tt_auto_post_service -v`：31/31 通过。
- `python -B -m unittest discover -s scripts -p 'test_tt*.py' -v`：560/560 通过。
- `python -m py_compile features/tt_auto_posts/publisher.py features/tt_auto_posts/service.py scripts/test_tt_auto_post_publisher.py scripts/test_tt_auto_post_service.py`：通过。
- `git diff --check`：通过。

## 回归结论

本地回归通过；生产配置切换、stale task 终态化和自然调度验收待部署后补充。
