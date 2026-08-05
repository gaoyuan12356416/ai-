# TT Post 发布池素材制作去除 Logo

## 目标

TT Post 发布池的正式 `direct_outro` 素材在制作时不再给正片左上角叠加 DramaWave Logo。固定教程片尾、Drama ID、声音处理和 `phone-match-0.9s` 转场保持不变。

## 范围

- 新制作的正式发布池素材使用无 Logo 的 `direct_outro` v2 profile。
- 旧 `direct_outro` v1 待发布素材不得再被自动或手动调度选中。
- 现有 `available` v1 素材需重制为 v2，并原子更新发布池与对应的 ready 入池账本。
- `branded_preview` 是历史预览模式，仍保留显式 Logo 合成能力且继续禁止正式 Direct Post。
- 不改变已经 `reserved`、`consumed`、`canceled` 或已经进入发布队列的历史记录。
- 本次验证不得主动创建 TikTok 发布请求。

## 验收标准

1. `direct_outro` 的 FFmpeg 最终合成命令不包含 Logo 输入、`scale=132:132` 或 `overlay=48:72`。
2. 教程片尾和居中缩小转场仍存在，最终 profile 为 `tt-post-direct-outro-hevc-720x1280-v2`（H.264 对应 v2）。
3. CPU 调度仅领取与当前 `TT_POST_MEDIA_PROFILE_VERSION` 完全一致的发布池素材。
4. 重制脚本默认 dry-run；只有 `--apply` 才调用 GPU，并且只迁移未占用的 `available` 精确源 profile。
5. 每条迁移先得到并校验新成片，再在一个 SQLite 事务中同时更新发布池与入池账本；并发变化必须 fail closed。
6. 现网旧 v1 available 数量归零，新 v2 available 数量与迁移前待发布数量一致；发布历史和 `publish_id` 数量不变。

## 回滚边界

- 代码回滚：CPU/GPU `current` 软链切回部署前 release，并恢复对应环境变量。
- 数据回滚：使用部署前 SQLite 在线备份恢复；不能只把 profile 字段改回 v1，因为成片指纹、URL、任务 ID 和请求指纹是一个整体。
- 已生成但未入账的新 COS 成片不影响发布状态，可后续按审计结果清理。
