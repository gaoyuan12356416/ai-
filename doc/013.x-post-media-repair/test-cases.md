# 013.x-post-media-repair 测试用例

| 编号 | 场景 | 预期 | 级别 |
| --- | --- | --- | --- |
| TC-001 | 原 H264/尺寸合格 | 不调用 GPU，原 URL 进入计划 | P0 |
| TC-002 | HEVC | 触发一次 repair，最终 H264/yuv420p | P0 |
| TC-003 | 1080×1920 | 输出 720×1280，比例保持 | P0 |
| TC-004 | 横版/方版 | 分别输出 1280×720 / 720×720 | P1 |
| TC-005 | 极端比例 | 等比缩放并补边，不裁剪/拉伸 | P1 |
| TC-006 | 无音轨 | 补 AAC-LC 静音轨并通过 probe | P0 |
| TC-007 | duration >140 秒 | 固定保留开头 139 秒，输出不超过 140 秒 | P0 |
| TC-008 | 同批超过六条需修复 | 第七条不调用 GPU，按不足处理 | P1 |
| TC-009 | CPU/GPU source SHA 或 size 不同 | fail closed | P0 |
| TC-010 | worker output SHA/size 与 CPU 下载不同 | fail closed | P0 |
| TC-011 | worker output URL 非 HTTPS/有凭据/重定向 | fail closed | P0 |
| TC-012 | worker probe 字段不完整或越界 | fail closed | P0 |
| TC-013 | repair 输出再次 probe 失败 | 不递归，继续后续 FIFO | P0 |
| TC-014 | 三条最终不足 | 只记 failed_preflight，零 queue/log/Post | P0 |
| TC-015 | 同 job 重复/并发 | 只编码上传一次，其余复用 manifest | P0 |
| TC-016 | COS HEAD/大小校验失败 | 不落可复用 manifest | P0 |
| TC-017 | 源 URL 非 allowlist/超大/下载中断 | worker fail closed | P0 |
| TC-018 | Bearer 缺失/错误 | 403，不启动下载/ffmpeg | P0 |
| TC-019 | worker 非 loopback bind 配置 | 启动失败 | P0 |
| TC-020 | legacy SQLite 重复迁移 | 新列默认空，迁移幂等 | P0 |
| TC-021 | repaired queue | material key 仍为原素材，最终 URL/审计字段冻结 | P0 |
| TC-022 | 发布前 COS 对象变化 | `media_preflight_changed`，零 X 请求 | P0 |
| TC-023 | 当前九条 backfill | 九条均上传新 COS 并通过 probe，零 queue/log/Post | P0 |
| TC-024 | 服务回滚 | 保留 SQLite/Token/COS，旧 daily 不消费修复字段 | P1 |
| TC-025 | duration <0.5 秒或 NaN/Inf | worker 拒绝，不启动 ffmpeg/COS | P0 |
| TC-026 | codec/dimensions 首错且源同时超长 | 同次规范化并裁尾至 139 秒 | P0 |
| TC-027 | 正常时长 codec/dimensions 修复 | 不带 `-t`，输出继续保持源时长 | P0 |
| TC-028 | validation_failed 短剧成功重验 | 先 dry guard；全链成功后原地 pending，ID/FIFO/进度不变 | P0 |
| TC-029 | 短剧绑定/历史/旧错误/集数任一不符 | 恢复冲突，保持原状态 | P0 |
| TC-030 | 短剧恢复命令 | 共享调度锁，零 plan/queue/log/Post | P0 |
