# 部署文档

## 变更内容

GPU TT worker 增加 `cos|local` 媒体后端。`local` 模式把 prepare 成片
持久化在 GPU `/data`，由独立 loopback 媒体端口提供 `GET`、`HEAD` 和
单段 Byte Range，公网 Nginx 只负责 HTTPS 终止和固定 prefix 反向代理。
TikTok publish 仍使用 `PULL_FROM_URL`，不改为 `FILE_UPLOAD`。

本变更默认保持 `TT_POST_GPU_STORAGE_BACKEND=cos`，也保持三项 Direct
Post 门禁为 0。公网入口未完成 DNS、80/443、安全组、可信证书和 TikTok
URL Property 前，不得把生产配置切换为 `local`，不得调用真实 TikTok
init。现有带 Logo/推广片尾 profile 继续
`direct_post_eligible=false`。

## 配置项

GPU root-only 环境在既有配置上增加：

```text
# 显式后端；默认 cos。公网条件未满足前生产保持 cos。
TT_POST_GPU_STORAGE_BACKEND=cos

# local 媒体进程只绑定 loopback，且端口必须与控制端口不同。
TT_POST_GPU_MEDIA_HOST=127.0.0.1
TT_POST_GPU_MEDIA_PORT=8831

# 仅在 local 后端必填；必须是无 path/query/fragment 的可信 HTTPS origin。
TT_POST_GPU_LOCAL_MEDIA_ORIGIN=https://<verified-media-domain>
TT_POST_GPU_LOCAL_MEDIA_PREFIX=tt-post-media/v1

# 仅在 local 后端必填；URL-safe base64 编码的精确 32-byte root-only key。
TT_POST_GPU_LOCAL_URL_SIGNING_KEY_B64=<root-only>

# 明确终态之后的最短清理宽限；首版不得低于 3600 秒。
TT_POST_GPU_TERMINAL_MEDIA_GRACE_SECONDS=3600

# 新 local prepare 的磁盘安全水位；默认建议至少 10 GiB。
TT_POST_GPU_LOCAL_MIN_FREE_BYTES=10737418240
```

既有控制面和门禁保持：

```text
TT_POST_GPU_HOST=127.0.0.1
TT_POST_GPU_PORT=8830
TT_POST_GPU_WORK_ROOT=/data/tt-post-publisher
TT_POST_LIVE_ENABLED=0
TT_POST_DIRECT_AUDIT_APPROVED=0
TT_POST_URL_PROPERTY_VERIFIED=0
TT_POST_URL_PROPERTY_VERIFIED_ORIGIN=
```

`TT_POST_URL_PROPERTY_VERIFIED_ORIGIN` 必须填当前后端已在 TikTok 验证的
规范 HTTPS origin：`local` 对比 `TT_POST_GPU_LOCAL_MEDIA_ORIGIN`，
`cos` 对比 `TT_POST_GPU_COS_DOMAIN`。三个原门禁即使都是 1，只要该值
为空或与当前后端 origin 不完全一致，GPU `ready` 仍为 false。切换后端
时必须重新核对，不能沿用上一后端值；没有外部验证证据时不得仅改环境值。

`cos` 后端继续要求既有 COS Secret、bucket、region、domain 和 prefix；
`local` 后端不要求 COS 凭据。配置必须按当前后端分支校验，禁止所有配置
都设为可选，也禁止 local 异常后自动回退/双写 COS。

HMAC key、COS 密钥、内部 Token 和凭据信封 key 均只保存在权限 600 的
root-only EnvironmentFile 中；禁止出现在 unit、命令行、Git、健康响应或
journal。首版未提供多 key 兼容时，有 pending/unknown local 任务不得直接
轮换签名 key。

### Nginx/TLS 合同

待域名与证书具备后，GPU 公网虚拟主机应满足：

- 443 使用可信完整证书链；80 只用于受控 ACME/HTTPS 跳转，TikTok 使用的
  媒体 URL 本身必须直接为 443 HTTPS 且不发生 30x。
- 仅固定 `/<media-prefix>/` 代理到 `127.0.0.1:8831`；其他路径返回 404。
- 不代理 `/internal/tt-post/*`，不提供目录列表或静态 root。
- 保留 Range、Content-Range、Content-Length 和 HEAD 语义；不动态压缩
  MP4，不把 `416` 改写为 `200`，不缓存签名 URL。
- 根据 34.8 分钟成片保留足够读取超时；客户端断连不重试写请求。

## 数据库变更

不修改 MySQL，不新增 CPU SQLite 表。local 文件生命周期记录在 GPU ready
manifest 与 publish ledger 中，采用只增字段并兼容旧 COS manifest：

- `storage.backend=cos|local`
- `storage.key`、冻结 URL、SHA 和大小
- publish ledger 的 `media_release.reason/release_after_epoch/state/released_at`

部署前备份 `/data/tt-post-publisher/manifests`、
`/data/tt-post-publisher/publishes` 和 GPU root-only 环境。不能删除或
重写已有 COS/local manifest，也不能把 unknown 任务迁移为另一后端。

## 部署前硬阻塞

以下五项必须逐项记录证据，任何一项缺失时只能部署保持 `cos`：

1. 计划媒体域名的 DNS A/AAAA/CNAME 最终指向受控 GPU 公网入口。
2. 云安全组、主机防火墙和监听已允许所需 80/443；控制端口 8830/8831
   仍不直接暴露公网。
3. 443 证书受信、域名匹配、证书链完整且有可持续续签方案。
4. 从 GPU 外部网络访问真实媒体 URL 无重定向，GET/HEAD/Range 正确。
5. 同一 origin 或精确 prefix 已在 TikTok Developer Portal 完成 URL
   Property 验证并归档证据，且 root-only
   `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN` 与当前后端 origin 精确一致。

这些是生产 `local` 和未来真实 Direct Post 的阻塞项，不得因为 loopback
测试、域名所有权或证书申请已提交就标记为完成。

## 部署步骤

1. 完成本地实现、自动化、SA 代码评审和测试报告；确认没有真实 TikTok
   init 调用。
2. 从工作分支推送 GitHub，记录精确 commit SHA。CPU/GPU 只允许从该 SHA
   构建 immutable release，不直接在服务器编辑源文件。
3. 记录 CPU/GPU 当前 release symlink、服务状态、三项门禁、当前后端和
   回滚 commit；备份 GPU 环境、Nginx、manifest、publish ledger。
4. 先以 `TT_POST_GPU_STORAGE_BACKEND=cos` 部署新 worker，三项门禁继续
   为 0；确认既有 COS prepare/复用、CPU 隧道和健康无回归。
5. 在临时测试根目录或不暴露公网的端口完成 local loopback canary：
   prepare、重启复用、GET/HEAD/Range、签名/路径拒绝、unknown 禁清理和
   终态冻结时钟用例。
6. 只有取得单独基础设施授权后，配置 DNS、安全组、Nginx 和证书。先从
   外部网络验证一个非生产测试文件，确认媒体 prefix 外路径和控制 API
   不可达。
7. 由 TikTok App 负责人完成 URL Property 验证并留存 origin/prefix 证据。
   对照证据填写 `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN`；在本轮仍保持
   `TT_POST_URL_PROPERTY_VERIFIED=0`。
8. 保持三项门禁为 0，把后端显式切为 `local`，重启 GPU worker；确认
   `/health` 只显示安全的后端/磁盘/门禁信息。
9. 以关闭态制作新的测试 job，记录文件路径、SHA、大小、URL、manifest
   和 COS 调用数 0；从外网验证完整 HEAD、首/中/尾/suffix Range。
10. 用素材 4665764 做 34.8 分钟关闭态 canary，验证低于 500 MB、既有
    profile/时长/音频合同、外网可读、无 COS 上传、TikTok init 计数为 0。
11. 演练新 job 的 `local -> cos` 回退：切回 COS 后只让新 job 使用 COS；
    旧 local 文件和账本原样保留。
12. 真实 Direct Post 仍不开放。未来如需开放，必须另起变更同时评审三项
    门禁、品牌片 `direct_post_eligible`、账号/内容合规和小流量 canary。

## 验证步骤

### 代码与服务

- GPU release symlink 与 GitHub SHA 完全一致，工作树无服务器热改。
- 控制端口 8830、媒体端口 8831 只监听 loopback；公网只暴露 Nginx 443。
- CPU health、GPU health、反向隧道和既有 TT runner/timer 正常。
- `/data` 可用空间高于水位；低于水位时新 prepare fail-close。
- health/journal 中无 HMAC key、TikTok Token、Authorization 或完整签名
  URL。manifest/SQLite 为保证幂等会冻结完整媒体 URL，但不得输出到日志，
  且继续受现有 root-only/后台访问控制保护。

### 媒体合同

- 同 job 重启后 URL、SHA、大小相同；文件/manifest 篡改均拒绝。
- loopback 和外网分别验证：

```text
HEAD full
GET bytes=0-1023
GET bytes=<middle>-<middle+1023>
GET bytes=-1024
GET out-of-range -> 416 bytes */<size>
```

- 所有成功 Range 与源文件切片逐字节一致；可信 TLS；无 30x。
- 路径穿越、错误签名、错误 SHA、软链接、POST/PUT 均不能读取或写入。
- `cos` 模式旧 manifest 和 local 模式新 manifest 各自精确复用，不混写。

### 生命周期与门禁

- 待发布、处理中、unknown 和 needs_review 文件在重启/清理循环后仍存在。
- 明确终态 3599 秒仍存在；达到 3600 秒且身份一致后才可幂等清理。
- 清理只影响精确 job 文件，不递归删除 media root，不触碰相邻 job。
- 三项门禁均为 0；门禁关闭不消费 pool、不创建可执行 queue。
- 纯 Mock 中把三个原门禁都设为 1、verified origin 留空或配置为另一
  后端 origin，GPU `ready` 必须仍为 false，TikTok init 调用数仍为 0。
- 再在纯 Mock 中保持配置门禁 ready，但把 prepared manifest URL 改成
  不同 origin；GPU publish 必须返回 `tt_publish_url_property_mismatch`，
  TikTok init 调用数仍为 0。
- 品牌 profile 为 `direct_post_eligible=false`；TikTok init 实际调用数 0。

## 回滚方案

### local 链路回滚

1. 保持三项发布门禁为 0，停止产生新的 local prepare。
2. 备份当前环境和账本，将
   `TT_POST_GPU_STORAGE_BACKEND=cos`，恢复/复验既有 COS 配置。
   如仍有 local pending/processing/unknown，必须继续保留原 local
   origin、prefix、签名 key、媒体端口和 Nginx，使已冻结 URL 仍可读取；
   只让新 prepare 改用 COS。
3. 重启 GPU worker，确认控制 health、COS 关闭态 prepare 和 CPU 隧道。
4. 旧 local manifest、文件和 publish ledger 原样保留；不得批量删除，
   不得把 URL 改写为 COS，也不得对 unknown 任务重新 init。
5. Nginx 媒体虚拟主机可在所有 local pending/unknown 均明确处置后再下线；
   不能因切回 COS 立即让旧 URL 失效。

### 代码 release 回滚

1. 停止 GPU TT worker，将 release symlink 切回部署前精确 SHA。
2. 恢复部署前 root-only 环境、systemd/Nginx 备份并重新 daemon-reload。
3. 启动服务并验证旧 COS health/prepare、CPU 隧道、TT timer 与 X 回归。
4. 新版 manifest/账本保留审计，不降级重写；若旧 release 无法安全识别
   local manifest，保持这些 job 冻结并人工核对。
5. 记录回滚时间、原因、影响 job、文件 SHA 和最终服务状态。

## 注意事项

- 禁止为了验证本需求临时打开任一发布门禁或使用真实 TikTok init。
- 禁止把 `direct_post_eligible=false` 改为 true 来绕过品牌内容审查。
- 禁止在公网 Nginx 暴露 8830、8831、`/internal/`、manifest 或目录列表。
- 禁止通过删除 pending、处理中、unknown 或 needs_review 文件解决磁盘告警。
- 禁止在同一 job 中 local/COS 双写，或在后端失败时静默换 URL。
- local 文件不再依赖 COS，但仍依赖 GPU 数据盘可靠性、备份、带宽、TLS
  和域名持续可用；生产监控和容量评估是上线必要条件。
