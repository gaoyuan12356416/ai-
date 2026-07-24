# 013.x-post-media-repair 需求与技术设计

## 背景

X 素材池已在发布计划创建前执行真实媒体预检。当前部分 Dramawave 源视频虽然内容和时长合格，但因 HEVC、尺寸超过 1280 或宽高比不符合现有 X 发布契约而被标记为 `invalid_media_codec` / `invalid_media_dimensions`。这些问题属于可确定修复的媒体封装与规格问题，不应与违规、素材不存在或元数据异常混为一类。

## 目标

- 仅当 X daily 预检返回 `invalid_media_codec` 或 `invalid_media_dimensions` 时，把原视频提交给 GPU 专用修复 worker。
- GPU 在不裁剪内容、不覆盖原文件的前提下生成 H264/yuv420p、逐行、CFR 30fps、AAC-LC 的 X 合格 MP4，上传到新的 COS 对象。
- CPU 对 worker 返回的 COS 文件重新下载、校验 SHA/大小并执行同一套 `probe_media`；只有重验通过的 URL 才能进入 queue。
- 原素材 ID 继续作为全局排重键；修复后的 COS 文件不能被当成新素材重复发布。
- 保存原 URL、触发原因、修复 job/profile 和源文件 SHA，保证发布日志可追溯。
- 当前九条未绑定的格式/分辨率失败素材先完成修复和复检，但不创建当日重复 queue，不额外调用 X 发帖。

## 范围

### 包含

- 独立 GPU worker、独立 Bearer、loopback HTTP 和 CPU→GPU SSH 反向隧道。
- 安全源下载、源 SHA/大小复核、NVENC 转码、无音轨补静音、等比缩放和补边。
- content-addressed COS 对象、原子 manifest、进程锁和幂等复用。
- daily runner 在计划前的一次性自动修复、返回文件二次下载/探测、失败后按 FIFO 继续补位。
- `x_post_queue` 增量审计字段和旧 SQLite 的幂等迁移。
- 离线回归、GPU/COS 实测、当前九条 backfill 与生产运行检查。

### 不包含

- 不修改 `ads_custom_source.url`，不覆盖原 COS 对象。
- 不修复违规、素材不存在、URL/下载、时长、帧率、扫描方式或其他错误。
- 不在 queue 创建后或发布阶段自动替换媒体。
- 不把现有剧集拼接 `/api/gpu-video/render` 当成通用转码接口。
- 不因修复完成而绕过账号日唯一约束、全局素材排重或 unknown-outcome 禁重放规则。

## 业务规则

1. 自动修复只允许两个触发码：`invalid_media_codec`、`invalid_media_dimensions`。
2. 每次 daily 最多自动修复六条，防止异常素材耗尽 GPU 和调度窗口。
3. worker 只监听 GPU 回环地址；CPU 只通过 `127.0.0.1` 反向隧道访问，禁止公网直连。
4. daily 与 worker 使用独立 `X_POST_MEDIA_REPAIR_TOKEN`，不得复用 X OAuth Token 或既有全功能 GPU worker Token。
5. CPU 首次下载取得的 `source_sha256/source_size` 必须与 GPU 二次下载一致，否则按源文件变化失败关闭。
6. 输出固定为新的 MP4 对象：竖版 720×1280、横版 1280×720、近方形 720×720；保持比例并补黑边，不裁剪、不拉伸、不生成新画面。
7. 输出视频为 H264 High、yuv420p、progressive、CFR 30fps、闭合 GOP 60；音频为单 AAC-LC、48kHz、双声道、128kbps；无音轨时补静音；MP4 使用 faststart。
8. worker 上传后必须校验 COS 对象，CPU 还要重新下载并核对 worker 回执的 SHA/大小，再运行正式 X probe。
9. worker 不得递归修复；输出重验失败即记录失败并继续后续 FIFO 素材。
10. 只有三条最终素材全部通过，daily 才以既有单事务创建三条 queue。
11. queue 的 `material_key/material_id/pool_item_id` 始终指向原素材；`material_url` 保存最终 COS URL，`preflight_sha256/preflight_size` 保存最终文件指纹。
12. 同一 profile、素材和源内容使用确定性 job/COS key；重复调用只复用已完整校验的对象和 manifest。
13. 已被 queue 引用的修复对象不得被临时文件清理或生命周期策略删除。
14. worker、隧道或 COS 不可用时 fail closed；不能回退为发布原始不合格视频。

## 数据结构

`x_post_queue` 增量字段：

| 字段 | 说明 |
| --- | --- |
| `original_material_url` | 修复前的源 URL；未修复为空 |
| `media_repair_trigger_code` | 两个允许触发码之一 |
| `media_repair_job_key` | content-addressed GPU job 标识 |
| `media_repair_profile` | 固定转码配置版本 |
| `media_repair_source_sha256` | CPU/GPU 一致确认的源内容 SHA |

现有 `material_url` 和 `preflight_sha256/preflight_size` 继续冻结最终发布文件及其指纹，不新增第二套发布身份。

## 流程

1. selector 按素材池 FIFO、Dramawave、违规记录和 X 标签规则取得候选。
2. CPU 下载原视频并取得源 SHA/大小。
3. 原视频 probe 成功则不调用 GPU；仅两个允许错误码进入修复。
4. CPU 调用回环 repair endpoint；GPU 再下载并核对源指纹。
5. GPU 按固定 profile 转码、probe、上传 COS、HEAD 校验并原子写 manifest。
6. CPU 下载返回的 COS 文件，核对输出 SHA/大小并运行正式 probe。
7. 成功候选携带修复审计字段进入既有三条计划；失败候选记录错误并继续 FIFO 补位。
8. 发布阶段仍按 queue 冻结 URL 再下载、核对 preflight 指纹并 probe；变化即零 X 请求失败。

## 验收标准

- 两个允许错误码可修复；其他错误 GPU 调用数为 0。
- 当前九条素材生成新的公开 HTTPS COS URL，全部通过正式 X probe。
- 修复过程不修改源库、不覆盖原 COS、不创建 queue/log/Post。
- 下一次自然 daily 可使用 worker 返回的最终 URL，且 queue 保留原素材排重身份和修复审计。
- worker/COS/隧道失败、源变化、回执不一致、输出不合格均 fail closed。
- 本地与服务器 X 回归、SQLite integrity/duplicate 检查、服务状态和备份/回滚检查全部通过。
