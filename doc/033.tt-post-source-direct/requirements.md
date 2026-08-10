# 033.TT Post 原片直发模式需求与技术设计

## 背景

当前 TT 社媒发布池使用 `direct_outro`：素材先在 GPU 端转码、追加固定教程片尾和转场，再由 TikTok `PULL_FROM_URL` 发布。运营需要临时测试“不制作、直接发原片”的效果，同时保留现有制作代码，便于后续切回或继续调整。

生产现状确认：

- CPU release：`4362f3928e8c5c3f437917585b9f645e51986536`。
- GPU release：`f1a0434443751646b848a5d931781ce9a404e511`。
- 原素材域名为 `advertising-1306474899.cos.ap-hongkong.myqcloud.com`，当前已验证的 TikTok URL Property origin 为 `https://socialkit-cdn.yingliang.tech`，因此不能直接把原始 COS URL 交给 TikTok。
- 生产只读探测已确认原片可能为 H.264/AVC `avc1` 或 HEVC/H.265 `hvc1`；两类均可为 720×1280、30fps、AAC 双声道，采样率为 44.1kHz 或 48kHz。

## 目标

1. 新增 `source_direct` 模式：不裁尾、不叠图、不加片尾、不做转场、不转码。
2. 原片下载后只做格式/时长/大小/哈希校验，并将完全相同的字节镜像到已验证的拉取域名。
3. 用独立 profile `tt-post-source-direct-v1` 冻结任务身份；旧 `direct_outro` 素材和代码完整保留。
4. 通过 CPU/GPU 两项配置切换模式，不改数据库结构、不批量改历史记录。
5. 保留既有发布门禁、Creator Info 校验、发布幂等账本、未知结果只核对不重发规则。

## 范围

### 包含

- GPU worker 新媒体模式、profile、原片校验、原字节镜像和 manifest v6。
- CPU/GPU 配置示例、定向自动化测试、部署与回滚说明。
- 将生产 CPU/GPU 配置切到 `source_direct`。

### 不包含

- 不删除或改写 `branded_preview`、`direct_clean`、`direct_outro`。
- 不迁移既有 91 条 ready 素材，不修改其 URL、SHA、profile 或状态。
- 不替用户触发真实 TikTok 发布。
- 不新增数据库字段或迁移 SQL。

## 业务规则与流程

1. 新入池或立即测试任务仍先持久化并由独立 prepare runner 处理。
2. GPU 校验 `expected_profile=tt-post-source-direct-v1` 和 `source_trim_tail_seconds=0`。
3. GPU 下载原片、计算 SHA-256/大小并执行 ffprobe；不调用 FFmpeg。
4. 只接受 720×1280、30fps、H.264 `avc1`（Baseline/Constrained Baseline/Main/High）或 HEVC `hvc1`（Main）、`yuv420p`、AAC-LC 双声道、44.1/48kHz，且满足现有大小、时长和平均码率上限的素材。编码与容器 tag 必须严格配对。
5. GPU 将原片字节原样上传到当前已验证的存储 origin，并在 manifest 中冻结源 SHA/大小、输出 SHA/大小、profile、mode 和 URL 哈希。
6. 发布前再次校验 manifest、实际输出 origin、三项生产门禁和 TikTok Creator Info。
7. 当前 profile 只领取同 profile 的素材。切到 `source_direct` 后，旧 `direct_outro` 素材保留但不被新调度领取；切回旧 profile 后可继续使用。

## 技术设计

### 影响模块

- `features/tt_gpu/worker.py`
- `scripts/test_tt_gpu_worker.py`
- `deploy/tt-post.env.example`
- `deploy/tt-post-gpu.env.example`

### 数据结构

无 schema 变更。沿用 `preparation_profile`、`prepared_output_sha256`、`prepared_output_size`、`prepared_media_url` 和 GPU manifest；新 manifest 版本为 6。

### API

接口路径和字段不变。`POST /internal/tt-post/prepare` 的 `expected_profile` 新增合法值 `tt-post-source-direct-v1`；健康接口将返回 `media_mode=source_direct`、`profile=tt-post-source-direct-v1`、`transition=none`。

### 异常与边界

- 非零裁尾：`source_direct_trim_forbidden`，下载前拒绝。
- profile 不一致：`prepare_profile_mismatch`，下载前拒绝。
- 原片媒体合同不满足：`prepared_media_invalid`，不发布。
- 输出 SHA/大小与源不一致或 manifest 漂移：`prepared_media_invalid`。
- 输出 origin 与已验证 URL Property 不一致：`tt_publish_url_property_mismatch`。

## 验收标准

- 自动化证明 `source_direct` 路径没有任何 FFmpeg 命令。
- 输出 SHA-256 和大小分别与下载后的源 SHA-256 和大小完全一致。
- manifest v6 冻结 mode/profile/源身份，精确重放复用，同 job 漂移拒绝。
- 正常发布 mock 仍走 `PULL_FROM_URL`，且既有三门禁/账本行为不变。
- 全量 TT 相关测试通过；生产服务健康且未因部署主动产生 TikTok init。
- 可用两项配置恢复 `direct_outro`，无需删除或迁移数据库记录。

## 风险与待确认

- `source_direct` 仍需一次下载和镜像，因为原始 COS origin 不是当前已验证 URL Property；这是传输，不是视频制作。
- 未来出现非 720×1280、非 H.264/HEVC、非 `yuv420p` 或非 AAC 标准素材时仍会 fail-closed，需要切回制作模式或扩展并评审原片合同。
- 真实帖子效果与 TikTok 最终处理结果由用户的手动测试确认。

## 变更记录

- 2026-08-07：初版，采用独立 `source_direct` profile 和原字节镜像方案。
- 2026-08-07：根据生产素材 `6028067` 的只读 ffprobe 证据，将原字节合同扩展为严格配对的 H.264/`avc1` 与 HEVC/`hvc1`，不引入转码。
