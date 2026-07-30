# 021.tt-post-gpu-local-pull 需求与技术设计

## 背景

当前 TT Post 流程在 GPU 数据盘完成视频制作后，将成片上传到 COS，再把
公网 HTTPS 地址交给 TikTok Content Posting API 的 `PULL_FROM_URL`。该
中转会增加一次大文件上传、COS 存储和流量，也让一次发布同时依赖 GPU、
COS 和 TikTok 三段链路。

本需求将“待 TikTok 拉取的成片”直接持久化在 GPU 数据盘，由 GPU 上的
只读媒体服务通过 HTTPS 提供给 TikTok。COS 保留为显式回退模式，不做
隐式双写。CPU 继续负责页面、素材池、排期和账本；GPU 继续负责制作、
媒体托管及 TikTok API 请求。

这项改造只改变 `PULL_FROM_URL` 的媒体来源，不代表真实 Direct Post
获准上线。当前三项全局门禁必须继续关闭；带 DramaWave Logo 和推广片尾
的现有 profile 仍为 `direct_post_eligible=false`。

## 目标

1. GPU 成片完成后可直接形成稳定的 GPU 本地 HTTPS 拉取地址，无需上传
   COS，也无需把视频传回 CPU。
2. 媒体服务支持 TikTok 拉取所需的 `GET`、`HEAD` 和单段 Byte Range，
   全程不重定向并返回正确的长度与范围响应。
3. 本地成片、manifest、发布账本和公开 URL 使用同一不可变身份；服务
   重启后仍可恢复，不以进程内存作为事实来源。
4. 明确安全生命周期：准备和待发布期间保留；init/结果未知时禁止清理；
   只有 TikTok 明确终态后才进入带安全宽限期的清理。
5. 保留 `cos` 存储后端作为受控回退，切换失败时可恢复现有行为。
6. DNS、80/443、可信证书和 TikTok URL Property 未全部完成前，local
   模式不得作为生产 Direct Post 来源，且不得开启任何发布门禁。

## 范围

### 包含

- GPU worker 增加 `local` 与 `cos` 两种显式媒体存储后端。
- `/data/tt-post-publisher` 下的本地成片持久化、原子落盘、SHA-256 与
  文件大小复验。
- GPU loopback 媒体服务及其公网 HTTPS 反向代理合同。
- 不可猜测、不可篡改的媒体 URL，以及路径、manifest、文件身份复验。
- `GET`、`HEAD`、完整下载、单段 `Range`、`206` 和 `416`。
- TikTok prepare/publish/reconcile 对本地 URL 的复用与终态清理策略。
- 磁盘余量保护、恢复、审计、部署与 COS 回退。
- 保持既有 CPU 页面、素材池、排期和 TikTok Token 信封流程兼容。

### 不包含

- 开启 `TT_POST_LIVE_ENABLED`、`TT_POST_DIRECT_AUDIT_APPROVED` 或
  `TT_POST_URL_PROPERTY_VERIFIED`。
- 将现有带 Logo/推广片尾的 profile 改成可直接发布。
- 代替负责人完成 TikTok App 审核、URL Property 验证或内容合规批准。
- 购买、解析或修改生产 DNS；开放云防火墙、安全组、80/443 或签发证书，
  除非另有明确的基础设施授权。
- 修改 TikTok 个号 Token 来源、CPU 调度状态机、X 发布池或素材库规则。
- 将媒体服务变成通用文件服务器、目录浏览、上传服务或永久 CDN。
- 清理历史 COS 对象或迁移已冻结任务的历史 URL。

## 用户故事 / 业务规则

1. 运营人员预览或入池时，GPU 仍按确定性 job 制作成片。选择
   `local` 时，成片原子写入 GPU 数据盘并返回 HTTPS URL；CPU 只
   保存 URL、SHA、大小、时长和 profile，不接触视频字节。
2. 一个 ready job 的 URL 必须由规范化 `job_id`、成片 SHA-256 和
   HMAC 签名共同确定。更换源、profile、Logo、片尾或输出 SHA 后不能复用
   旧 URL。
3. 媒体 URL 只能读取与 manifest 完全一致的普通文件。路径穿越、软链接、
   非法编码、签名错误、job/manifest SHA 或大小不一致均 fail-close。
   固化、ready 复用和 publish 前执行完整内容 SHA；每次 GET/HEAD 复验签名、
   manifest、普通文件和 fstat 大小，不为每个 Range 重算整部视频 SHA。
4. 公网访问只允许 `GET` 和 `HEAD`。完整请求返回 `200`；合法单段 Byte
   Range 返回 `206`、`Content-Range` 和精确 `Content-Length`；越界返回
   `416 bytes */<size>`。多段 Range 不在首版范围内，明确拒绝。
5. 响应必须包含 `Accept-Ranges: bytes`、`Content-Type: video/mp4`、
   `X-Content-Type-Options: nosniff` 和 `Cache-Control: no-store`。
   `HEAD` 返回与对应 `GET` 相同的状态和响应头，但不返回 body。
6. 公网 URL 必须为可信 HTTPS、无重定向、可从互联网访问，域名或 URL
   prefix 必须先在 TikTok Developer Portal 完成 URL Property 验证。
7. `local` 文件不是临时工作目录文件。prepare 完成后即使 worker
   重启也必须存在；prepare 失败产生的临时文件才可安全清除。
8. 没有调用 TikTok init、任务尚未到点或正在等待发布时，文件必须保留。
   init 结果未知、网络结果未知、已有 `publish_id` 但状态未知、任务为
   `needs_review` 时禁止清理，也禁止重新 init。
9. 只有 TikTok 状态接口明确返回 `PUBLISH_COMPLETE` 或
   `FAILED/PUBLISH_FAILED` 后，文件才可标记为可清理；
   最早清理时间不得早于终态时间加一小时安全宽限。清理动作必须验证
   manifest/账本身份并以幂等方式记录。`init_outcome_unknown`、非终态、
   `init_rejected`、账本缺失或身份冲突时不删。首版不把 init HTTP
   错误当作可自动清理证明；5xx、408、409、425、429 必须归为 outcome
   unknown。
10. 磁盘可用空间低于配置水位时，新 `local` prepare 必须在下载或
    转码前拒绝。入场预算至少为保留水位加配置的最大 source、最大 output
    和 512MiB 中间文件余量；进程内新 prepare 串行通过该预算，不得让多个
    请求同时看见同一份空闲空间。不得通过删除 unknown 或待发布成片腾空间。
11. `cos` 与 `local` 是互斥后端。单次 prepare 只写一个后端，不
    双写；切换后既有 manifest 继续按其冻结的 `storage_backend` 读取。
    切回 COS 时若仍有 local pending/unknown，local 只读服务和原签名 key
    必须继续存续，不能让历史 URL 随后端切换失效。
12. COS 回退只能由 root-only 配置显式切换并重启服务。配置缺失、来源
    URL 不符合当前后端或复用 manifest 漂移时都必须拒绝，不得静默降级。
13. 三项 Direct Post 门禁任何一项为 0 时，runner 不消费发布池、不创建
    可执行 queue、不调用 TikTok init。GPU `/publish` 同样再次 fail-close。
14. 即使三项门禁全部误开，`direct_post_eligible=false` 的品牌成片仍
    必须在 TikTok init 前被拒绝。
15. `TT_POST_URL_PROPERTY_VERIFIED=1` 只表示人工勾选，不足以打开门禁。
    `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN` 必须与当前后端实际 origin
    精确一致：`local` 对比 local media origin，`cos` 对比 COS domain。
    三项原门禁即使都是 1，只要 origin 为空或不匹配，`ready` 仍为 false。
    GPU publish 还必须逐次解析当前 prepared URL 的实际 HTTPS origin 并
    与 verified origin 精确比较，防止旧/篡改 manifest 绕过后端配置绑定。

## 交互与流程

1. CPU 以现有内部认证调用 GPU `prepare`，请求中继续携带确定性
   `job_id`、`content_id`、源信息和 `expected_profile`。
2. GPU 在当前配置后端下查找 ready manifest。身份一致且文件仍可读时
   返回既有结果；不一致时返回幂等冲突，不静默重做或换 URL。
3. 新 job 在工作目录完成下载、剪辑和探测，经 SHA/大小/profile 校验后，
   以原子 rename 固化至 local media root，随后原子写 ready manifest。
4. 公网 Nginx 只终止 TLS，并把固定媒体 prefix 代理到
   `127.0.0.1` 的专用只读媒体端口；控制 API 仍只通过原有 loopback 和
   CPU↔GPU 隧道访问。
5. 关闭门禁状态下，仅允许 prepare 和外部媒体读取验收，不调用 TikTok。
6. 合规条件未来全部确认后，GPU 先逐次复验 prepared URL 的实际 HTTPS
   origin 与 verified origin，再把同一 URL 放入 TikTok
   `PULL_FROM_URL`。拿到明确 `publish_id` 后只 reconcile，不重复 init。
7. reconcile 得到明确终态或 init 明确被拒绝后，在 ledger 的
   `media_release` 中冻结原因和 `release_after_epoch`。清理器到期后再次
   核对状态、manifest 与文件身份，再删除本地媒体并保留 released 审计。
8. local 公网链路异常时先保持门禁关闭；必要时切回 `cos` 后端。既有
   local 任务不得改写为 COS URL，除非建立新的确定性 job 身份。

## 技术设计

### 影响模块

- `features/tt_gpu/worker.py`：后端配置、LocalMediaStore、只读媒体
  Handler、prepare 复用校验、发布后生命周期和健康状态。
- `scripts/tt_gpu_worker.py`：启动控制端口与 local media loopback 端口。
- `scripts/test_tt_gpu_worker.py`：配置、路径安全、Range、生命周期、COS
  兼容和门禁自动化。
- `deploy/tt-post-gpu.env.example`、GPU systemd 与 Nginx 配置：在开发
  完成后由主任务按 GitHub-first 流程另行修改、部署和验证。
- `features/tt_posts/service.py`：原则上无需改变 CPU prepare/publish
  合同；如增加 `storage_backend` 安全字段，只用于核对和审计。

### 数据结构

ready manifest 在保留既有 job/content/profile/source/asset/probe 字段基础
上增加或规范化：

- `storage.backend`: `local|cos`
- `storage.key`: 当前后端内的规范化不可变对象键
- `url`: 冻结的精确 HTTPS URL
- `output_sha256`、`output_size_bytes`: 文件身份
- `created_at`、`ready_at`
- publish ledger 的 `state/updated_at`，以及
  `media_release.reason/release_after_epoch/state/released_at`

本地推荐布局：

```text
/data/tt-post-publisher/
  manifests/<job_id>.json
  media/<job_id>/<sha256>.mp4
  jobs/<job_id>/...
  publishes/<job_id>.json
```

manifest 与发布账本均采用临时文件加原子 rename 写入。清理只删除
`media/<job_id>/<sha256>.mp4`，不得递归删除计算路径或触碰其他 job。

### API / 接口

- 既有控制面：
  - `GET /health`
  - `POST /internal/tt-post/prepare`
  - `POST /internal/tt-post/publish`
  - `POST /internal/tt-post/reconcile`
- 新增数据面：
  - `GET /<media-prefix>/<job_id>/<sha256>/<signature>.mp4`
  - `HEAD /<media-prefix>/<job_id>/<sha256>/<signature>.mp4`

公网数据面不接受 Bearer Token，不暴露 manifest；使用高强度 HMAC URL
能力令牌并校验 manifest/文件身份。签名密钥为 root-only，禁止出现在 URL、
日志、健康响应或 manifest 中。完整字段和错误合同见 `api-doc.md`。

### 异常与边界

- 配置后端为 `local` 时，本地 origin、loopback 端口、32-byte HMAC
  密钥、media root 任一缺失即启动失败。
- 配置后端为 `cos` 时，既有 COS 密钥、bucket、region、domain 任一缺失
  即启动失败；local 配置不应成为必填项。
- 同一 ready job 的后端、URL、SHA、大小、profile 或品牌资产漂移时返回
  幂等冲突。
- 仅接受一个规范 `Range: bytes=...`；超范围 `416`，语法错误或多段请求
  明确拒绝；不得回退为错误的全量 `200`。
- 客户端中断 Range 读取只终止本次响应，不改变文件或任务状态。
- Nginx、媒体进程、DNS、证书或公网连通性异常都不得触发 COS 隐式双写，
  也不得打开发布门禁。
- `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN` 缺失或与当前后端规范化 HTTPS
  origin 不完全一致时，健康状态的 `ready` 必须为 false；后端切换后须
  重新匹配，不能沿用上一后端的验证声明。
- 即使健康门禁已 ready，publish 仍逐次从 prepared URL 解析实际 HTTPS
  origin；与 verified origin 不匹配时返回 property mismatch 并在 init
  前终止。
- 文件不存在但 manifest 仍为 ready 时返回不可重试的产物缺失错误并告警，
  不使用同 job 静默生成不同内容。
- unknown 状态没有自动过期删除；必须先通过 reconcile 或人工核验转为
  明确终态。

## 验收标准

1. `local` prepare 的视频只位于 GPU `/data`，无 COS Put/Multipart
   调用，CPU 也不接收视频字节。
2. 服务重启后同一 job 返回同一 URL、SHA 和大小；ready 复用或 publish
   前对文件做完整 SHA，文件与 manifest 任一被篡改时均 fail-close。
3. 公网 HTTPS URL 无重定向，完整 `GET`、`HEAD`、首段/中段/尾段/suffix
   Range 均返回正确状态、头和字节；非法与越界 Range 安全拒绝。
4. 路径穿越、签名篡改、错误 job/SHA、软链接和非普通文件不能读取。
5. 34.8 分钟生产成片可从公网 URL 完成首段、中段、尾段抽样，文件仍符合
   720×1280、HEVC、音频、时长和低于 500 MB 的既有交付合同。
6. `cos` 回退自动化与关闭态生产 canary 通过；切换后旧 COS manifest
   仍可复用，local 与 COS 不混淆。
7. 明确终态前文件不删除；unknown/needs_review 重启和清理循环后仍存在；
   明确终态加一小时后才允许幂等清理。
8. 低磁盘水位拒绝新 prepare，但不删除 unknown、待发布或正在发布文件。
9. 三项门禁始终为 0，品牌 profile 始终 `direct_post_eligible=false`；
   验收期间 TikTok init 调用计数为 0。
10. DNS 解析、80/443、安全组、可信证书、外网访问和 TikTok URL Property
    全部有可审计证据后，local URL 才能进入未来真实发布变更审批。
11. 纯 Mock 中即使三个原门禁均为 1，只要 verified origin 缺失或指向
    另一后端 origin，`ready` 仍为 false 且 TikTok init 调用数为 0；
    与当前后端规范化 HTTPS origin 精确一致时才满足该附加条件。另篡改
    prepared URL 为不同 origin 时，即使配置门禁 ready 也必须在 init 前
    返回 `tt_publish_url_property_mismatch`。

## 风险与待确认

- 当前生产 DNS、80/443、安全组、证书和 TikTok URL Property 均是上线
  阻塞项；任何一项未完成都只能做代码、loopback 或关闭态验收。
- 公网域名必须指向 GPU 或受控入口。若 GPU 公网出口/入口、云安全组或
  证书自动续签不可持续，应保持 COS 后端。
- 长视频拉取会占用 GPU 公网带宽和磁盘 I/O；正式放量前需核对并发、带宽、
  Nginx 超时、打开文件数和监控。
- 签名 URL 在文件存续期内属于访问凭据。不得写入公开页面、非受控日志或
  第三方分析系统；密钥轮换需保留旧 key 的受控兼容窗口或让旧任务继续用
  COS，不能使 pending URL 突然失效。
- 终态清理不能只依赖单次进程内 timer；需有可重放账本扫描。unknown 没有
  自动清理，可能长期占盘，必须配套告警和人工核对流程。
- TikTok 对 H.265 的通用规格支持不等于当前 App/账号/内容审核通过；真实
  发布仍需独立审批和小流量合规 canary。

## 变更记录

- 2026-07-30：建立 GPU local `PULL_FROM_URL`、COS 回退、媒体读取、
  生命周期、合规门禁及基础设施阻塞设计。
- 2026-07-30：完成代码实现、自动化回归和独立代码评审；生产公网链路与
  TikTok 外部验收仍受 DNS/TLS/安全组/URL Property 条件阻塞。
