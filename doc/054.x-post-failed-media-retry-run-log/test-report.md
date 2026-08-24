# 测试报告

## 离线回归

- `python -m unittest discover -s scripts -p "test_x*.py"`
- CPU/runtime 完整回归：`Ran 756 tests`，`OK (skipped=2)`。
- GPU COS 重试聚焦回归：`Ran 20 tests`，`OK`。
- `python -m py_compile features/x_posts/service.py scripts/x_post_failed_media_recovery.py`
- `git diff --check`

## 迁移与上线验收

- 备份库：`quick_check=ok`，外键错误 0。
- 迁移演练副本：新增 `x_post_schedule_failed_media_recovery_audit`，`quick_check=ok`，外键错误 0。
- 线上 combined runs API：最近一条为 `2026-08-24` schedule 批次 320。
- 线上服务：`x-post-automation.service=active`、`drama-material-api.service=active`。
- timers：schedule/claim/manual/X Auto 均为 enabled/active；daily 为 masked/inactive。
- runtime、主后台与 Nginx 静态文件与 Git release 内容一致。

## 一次性恢复验收

- 精确失败范围：runs 318、320，共 21 条；下发前仍全部为失败态，且不存在 Post ID、Post URL、relay Post ID 或 unknown outcome。
- 图片队列 602 已从 6,491,387-byte PNG 重制为 488,886-byte JPEG，保持 1440×1800（4:5），公开回读哈希一致。
- 视频检查点下发时为 6/20；GPU COS 瞬时失败增加重试与 HEAD 存在性校验，生产健康检查通过。
- 后台脚本 Git blob 与服务器文件一致，并通过服务器端 `py_compile` 和 CLI 参数解析检查。
- `x-post-failed-media-recovery-20260824.service` 已成功下发，首次日志为 `prepare_start existing=6 total=20`。
- 下发时 schedule/claim/manual/X Auto 五个 timer 均 active，旧 daily timer inactive；未等待最终发布完成，不能把“已下发”表述为“已发布成功”。
