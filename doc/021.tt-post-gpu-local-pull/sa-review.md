# SA 评审意见

## 结论

方案在架构上有条件通过：GPU 本地 HTTPS 媒体源可以替代 COS 作为 TikTok
`PULL_FROM_URL`，且不需要把成片传回 CPU。通过条件是实现必须保持
“控制面 loopback、数据面只读、不可变文件身份、unknown 不清理、COS
显式回退和发布门禁 fail-close”。

代码实现、自动化和独立代码评审已通过；本结论仍不表示生产网络或 TikTok
发布已经通过。DNS、80/443、安全组、可信证书和 TikTok URL Property
仍是生产上线阻塞；三项门禁继续为 0，品牌成片继续
`direct_post_eligible=false`。

## 问题清单

| 编号 | 严重级别 | 位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P0 | 公网媒体入口 | 直接暴露 worker 控制端口会同时暴露 prepare/publish 能力 | 控制 API 保持 `127.0.0.1:8830`；单独的媒体端口只绑定 loopback，Nginx 只代理固定媒体 prefix | 已实现并回归 |
| SA-002 | P0 | URL 安全 | 仅用 job/SHA 路径可被枚举，且路径篡改可能越权读取 | 使用 32-byte root-only HMAC 密钥签名规范路径；常量时间比较；不得记录密钥 | 已实现并回归 |
| SA-003 | P0 | 文件读取 | 路径穿越、软链接或 manifest/文件漂移可能读取任意文件 | 严格正则规范化，限定 media root；拒绝 symlink/非普通文件；读取前复验 manifest、SHA、大小 | 已实现并回归 |
| SA-004 | P0 | TikTok 拉取 | 不支持 `HEAD` 或 Byte Range、错误返回全量 `200` 会导致长视频拉取失败或浪费带宽 | 支持 `GET`/`HEAD` 和单段 open-ended/suffix Range；正确 `206`/`416` 与长度头；多段明确拒绝 | loopback 已实现并回归；公网待验 |
| SA-005 | P0 | 生命周期 | prepare 工作目录被清理后，返回 URL 可能立即失效 | 成片原子固化至 `/data/.../media`，与临时 job 目录分离；服务重启后可恢复 | 已实现并回归 |
| SA-006 | P0 | unknown outcome | init 响应或远端状态未知时清理文件，会让 TikTok 后续拉取失败，也可能促成重复 init | `unknown`、`needs_review`、非终态和账本冲突一律禁止清理；只 reconcile，不重发 | 已实现并回归 |
| SA-007 | P0 | 终态清理 | 一见终态就删，可能早于 TikTok 完成最后读取 | 明确成功/失败终态后至少保留一小时；清理前重读账本与 manifest；删除幂等并留审计 | 已实现并回归 |
| SA-008 | P0 | 合规门禁 | 改成本地 URL 可能被误解为已满足真实发布条件 | CPU claim 前和 GPU init 前保留三项门禁；品牌 profile 独立拒绝；关闭态验收 init 计数为 0 | 自动化通过；生产门禁保持关闭 |
| SA-009 | P0 | 基础设施 | 域名未解析、80/443 未开放、证书不可信或 URL Property 未验证时，TikTok 无法拉取 | 列为部署硬阻塞；在全部证据完成前只允许 loopback/关闭态 canary | 阻塞，待外部条件 |
| SA-010 | P1 | 后端切换 | local 异常时隐式双写/自动降级会产生两个 URL 和不确定事实 | `storage_backend` 写入 manifest；单 job 单后端；仅 root-only 配置显式切换 COS | 已实现并回归 |
| SA-011 | P1 | 密钥轮换 | 直接替换 HMAC key 会让 pending URL 全部失效 | 上线前确定轮换方案；未实现多 key 前不得在 pending 任务存在时轮换 | 待运维方案 |
| SA-012 | P1 | 磁盘容量 | 长视频积压、并发下载和 unknown 长期保留可能耗尽 `/data` | 新 prepare 进程级串行入场；要求 free 至少覆盖 reserve+max source+max output+512MiB；低水位 fail-close，禁止删除 pending/unknown 腾空间 | 已实现并自动化验证 |
| SA-013 | P1 | 兼容 | 把 COS 配置改为始终可选可能破坏现有后端启动验证 | 配置按所选后端分支校验；增加 `cos` 旧行为回归与旧 manifest 复用用例 | 已实现并回归 |
| SA-014 | P1 | HTTP 代理 | Nginx 缓冲、压缩或错误重写 Range 可能改变应用层响应 | 关闭重定向和动态压缩，透传 Range/长度；从外网对首/中/尾段逐字节验证 | 待部署验证 |
| SA-015 | P1 | 可观测性 | 只验证 200 健康无法发现签名、Range、磁盘或孤儿文件异常 | 健康响应提供无敏感信息的后端/磁盘状态；日志只记 job 摘要、状态和字节范围 | 已实现并回归 |
| SA-016 | P0 | URL Property 门禁 | 三个布尔门禁都为 1 时，切换 COS/local 后可能沿用上一 origin 的人工验证声明 | 增加 `TT_POST_URL_PROPERTY_VERIFIED_ORIGIN`；与当前后端规范化 origin 精确匹配才让 `ready=true` | 已实现并回归 |
| SA-017 | P0 | publish URL | 只比较配置后端 origin，仍可能让旧/篡改 manifest URL 绕过验证 | 每次 publish 解析 prepared URL 的实际 HTTPS origin 并与 verified origin 常量时间精确比较 | 已实现并回归 |
| SA-018 | P0 | init HTTP 结果 | 5xx/限流/超时类 HTTP 响应若被误记为明确拒绝，自动清理会中断 TikTok 可能仍在进行的拉取 | 5xx、408、409、425、429 归 outcome unknown；首版 `init_rejected` 也不自动清理，只有状态接口明确终态才释放 | 已实现并回归 |
| SA-019 | P1 | 文件打开 | 先 lstat 再按路径 open 存在检查/使用窗口，未来权限变化时可能跟随被替换链接 | 用 no-follow 打开并在同一 fd 上 fstat/seek/read；目录继续 root-only 0700 | 已实现并复核 |
| SA-020 | P1 | 完整性成本 | 每个 Range 都重算 300 MB SHA 会拖慢 TikTok 拉取；完全不验 SHA 又会让发布前腐坏不被发现 | 固化、reuse、publish 前全 SHA；每请求校验签名、manifest、普通文件和 fd size | 已实现并回归 |

## 决策记录

- 数据路径定为 `TikTok -> HTTPS/Nginx -> GPU loopback media server ->
  /data`；控制路径仍为 `CPU -> SSH reverse tunnel -> GPU control server`。
- local 媒体 URL 使用 HMAC 能力令牌，不用短时查询参数。URL 的有效性由
  文件生命周期控制，避免排期数天后签名先过期。
- 首版只实现单段 Range。多段请求明确拒绝，不做 multipart/byteranges。
- local 和 COS 是互斥后端；不做自动双写，不对已冻结 job 更换后端。
- 明确终态后的清理至少等待一小时；unknown/needs_review 没有时间到期
  自动删除，必须先核对远端状态。
- DNS、80/443、安全组、证书与 TikTok URL Property 是独立上线门槛；
  代码部署不能替代这些证据。
- URL Property 布尔值之外再绑定精确 verified origin；local/COS 切换后
  原 origin 自动失配并保持 fail-close。
- publish 不只信任后端配置，还逐次核对 prepared URL 的实际 origin。
- init HTTP 错误与 unknown fail-close：首版均不得驱动自动清理；只有状态
  接口明确终态可进入安全宽限。
- 真实 TikTok canary 不属于本需求关闭态验收；三项门禁和品牌门禁均保持
  关闭。

## PM 修订确认

2026-07-30 已将上述 P0/P1 设计建议写入需求、测试用例和部署方案。
代码实现、自动化和独立代码评审已完成；生产基础设施与外部 TikTok 验收
仍待执行，因此当前只能按 COS 和全门禁关闭方式安全部署，不能标记为
“local 可上线”或“可真实发布”。
