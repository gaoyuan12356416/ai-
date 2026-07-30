# 测试用例

## 测试范围

GPU local/COS 配置、成片持久化、不可变 URL、只读媒体 HTTP 合同、路径
安全、Range、prepare/publish/reconcile 生命周期、低磁盘保护、发布门禁、
部署切换与既有 TT/X 回归。

核心代码自动化已执行，结果见 `test-report.md`；表内未被现有自动化完整
覆盖的故障注入和所有外网/实片用例仍保持待执行。自动化不得使用真实
TikTok Token，不得调用真实 TikTok init；生产 canary 必须保持三项门禁为 0。

## 测试数据

- 临时 GPU work root、media root、manifest 和 publish ledger。
- 具有固定 SHA 的小型 MP4，以及边界大小/被篡改/软链接测试文件。
- 固定 32-byte 测试 HMAC key，不使用生产密钥。
- Mock TikTok init/status 响应：未调用、明确成功、明确失败、超时/unknown、
  `needs_review`。
- Mock COS 客户端，用于断言 local 无 COS 调用及 COS 回退兼容。
- 34.8 分钟素材 4665764 的生产关闭态成片用于外部首/中/尾 Range 验收；
  不以本地 fixture 冒充生产证据。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | local 配置完整 | `storage_backend=local`，origin/端口/media root/32-byte key 有效 | 加载配置并启动 | 控制端口和媒体端口均只绑定 loopback；COS 密钥不是必填 | P0 | 自动化通过 |
| TC-002 | local 配置缺失 | 分别缺 origin、签名 key、非法 prefix、非 loopback host | 加载配置 | 启动 fail-close，安全错误不含密钥 | P0 | 自动化通过 |
| TC-003 | COS 配置兼容 | `storage_backend=cos`，COS 配置完整，local 配置为空 | 加载配置并 prepare | 旧 COS 行为可用，local 配置不是必填 | P0 | 自动化通过 |
| TC-004 | COS 配置缺失 | `storage_backend=cos` 且任一 COS 必需项为空 | 加载配置 | 启动 fail-close，不自动改用 local | P0 | 待执行 |
| TC-005 | local 成片原子固化 | 有效临时成片 | 执行 prepare | 成片从 job 临时目录原子固化到 `/data/.../media`，manifest 最后落盘 | P0 | 自动化通过 |
| TC-006 | local 禁止 COS 调用 | Mock COS 对任一调用抛错 | 执行 local prepare | prepare 成功，COS Put/Multipart/HEAD 调用数均为 0 | P0 | 自动化通过 |
| TC-007 | prepare 确定性复用 | 同 job、源、profile、品牌资产和输出身份 | 连续 prepare 并重启后再 prepare | 三次 URL、SHA、大小一致，不重复制作/复制 | P0 | 自动化通过 |
| TC-008 | ready manifest 漂移 | 分别改后端、URL、job、content、SHA、大小、profile、Logo/片尾 SHA | 复用 prepare | 全部幂等冲突或产物不可信错误，不静默重做/换 URL | P0 | 自动化通过 |
| TC-009 | 文件缺失或篡改 | ready manifest 存在，文件被删、同尺寸改字节或改大小 | prepare/媒体读取/publish | reuse/publish 完整 SHA 可发现同尺寸篡改；读取拒绝缺失/类型/大小漂移；不调用 TikTok、不返回另一文件 | P0 | 自动化通过 |
| TC-010 | 完整 GET | 有效签名 URL | 请求无 Range 的 `GET` | `200`、完整字节、精确 Length、MP4、Accept-Ranges、no-store、nosniff | P0 | 自动化通过 |
| TC-011 | HEAD | 有效签名 URL | 请求 `HEAD` | 状态和头与对应 GET 一致，body 长度为 0 | P0 | 自动化通过 |
| TC-012 | 起始 Range | 有效文件 | 请求 `bytes=0-1023` | `206`、精确 1024 字节和 `Content-Range: bytes 0-1023/<size>` | P0 | 自动化通过 |
| TC-013 | 开放尾 Range | 有效文件 | 请求 `bytes=1024-` | `206`，返回从 1024 至 EOF 的精确字节 | P0 | 待执行 |
| TC-014 | suffix Range | 有效文件 | 请求 `bytes=-1024` | `206`，返回最后 1024 字节；大于文件大小时按全文件范围处理 | P0 | 自动化通过 |
| TC-015 | 中段与尾段 Range | 有效长视频 | 分别抽取中间和最后字节区间 | 状态、范围、长度和源文件切片逐字节一致 | P0 | 待执行 |
| TC-016 | 越界 Range | 起始位置等于或大于文件大小 | 请求 Range | `416`、`Content-Range: bytes */<size>`，无视频 body | P0 | 自动化通过 |
| TC-017 | 非法/多段 Range | 空范围、倒序、非数字、多段 | 请求 Range | 明确拒绝，不把错误请求降级成全量 `200` | P0 | 待执行 |
| TC-018 | 方法限制 | 有效 URL | 分别请求 POST/PUT/DELETE/OPTIONS | 不读取文件，返回方法不允许的安全响应 | P0 | 待执行 |
| TC-019 | HMAC 篡改 | 改 job、SHA、signature 任一字符 | GET/HEAD | 一律拒绝，响应不暴露文件是否存在或 manifest 内容 | P0 | 自动化核心路径通过 |
| TC-020 | 路径穿越与编码 | `..`、双编码、反斜杠、额外段、超长 segment | GET | 全部拒绝，media root 外文件未被访问 | P0 | 待执行 |
| TC-021 | 软链接与非普通文件 | 路径指向 symlink、目录、设备或 FIFO | GET | 全部拒绝，不跟随链接、不阻塞 | P0 | 待执行 |
| TC-022 | 客户端中断 | Range 下载中断开连接 | 再次请求并检查文件/账本 | 服务继续健康；文件和任务状态不变；无敏感堆栈 | P1 | 待执行 |
| TC-023 | 并发读取 | 多个并发 GET/Range 请求同一文件 | 并发执行 | 字节正确，无写入、删除或 manifest 竞争 | P1 | 待执行 |
| TC-024 | 低磁盘与并发入场 | 模拟 free 低于 reserve+max source+max output+512MiB，并并发两个不同 job | 新 local prepare | 在下载/转码前拒绝；不同 job 只可串行通过同一入场预算；现有 pending/unknown 文件不删 | P0 | 自动化通过 |
| TC-025 | 未 init 生命周期 | ready 成片但排期未到或门禁关闭 | 运行清理循环并重启 | 文件始终保留 | P0 | 待执行 |
| TC-026 | init unknown 禁清理 | init 超时/响应不明或账本 `needs_review` | 重启、重复清理、重复 publish | 文件保留；不再次 init；只允许 reconcile/人工核对 | P0 | 自动化核心路径通过 |
| TC-027 | 非终态禁清理 | 已有 publish_id，TikTok 状态仍处理中 | 多次 reconcile 和清理 | 文件保留，账本不生成 released 状态或 `released_at` | P0 | 自动化通过 |
| TC-028 | 明确成功终态清理 | TikTok 明确 `PUBLISH_COMPLETE` | 终态后 3599 秒/3600 秒运行清理 | 3599 秒保留；达到宽限且身份一致后删除并幂等记账 | P0 | 待执行 |
| TC-029 | 明确失败终态清理 | TikTok status 返回 `FAILED/PUBLISH_FAILED` | 按安全宽限前后运行清理 | 宽限前保留，宽限后身份一致才删除 | P0 | 待执行 |
| TC-030 | 清理身份冲突 | 终态账本与 manifest/job/SHA 不一致 | 运行清理 | 不删除，记录安全告警并进入人工核对 | P0 | 自动化通过 |
| TC-031 | 清理幂等与重启恢复 | 已成功清理或清理中进程退出 | 重启并重复扫描 | 不报致命错误，不删除其他 job，最终账本单一确定 | P0 | 自动化核心路径通过 |
| TC-032 | 三项门禁关闭 | 任一全局 gate=0 | 到点、手动、GPU publish | 不消费 pool、不创建可执行 queue、不调用 TikTok init | P0 | TT 全量自动化通过 |
| TC-033 | 品牌 profile 门禁 | 三项 gate Mock 为 1，但 `direct_post_eligible=false` | GPU publish | 在 init 前拒绝，TikTok 调用数为 0 | P0 | 自动化通过 |
| TC-034 | local publish 请求体 | 全部门禁与非品牌 test profile 在纯 Mock 中满足 | 调用 publish | TikTok Mock 收到 `source=PULL_FROM_URL` 和精确 local HTTPS URL；不使用 FILE_UPLOAD | P0 | 自动化通过 |
| TC-035 | COS publish 回退 | COS manifest ready | 调用纯 Mock publish | 请求仍使用冻结 COS URL；不被 local origin 重写 | P0 | 自动化通过 |
| TC-036 | 后端切换兼容 | 先生成 COS manifest，再以 local 配置启动；反向再测 | 复用旧 job/创建新 job | 旧 job 按冻结后端处理；新 job 用当前后端；不混用或双写 | P0 | 自动化通过 |
| TC-037 | 健康与日志脱敏 | local/COS 正常、低磁盘和请求错误 | 查看 health/journal | 可见后端与安全状态；无签名密钥、Token、Authorization、完整签名 URL | P0 | 自动化通过 |
| TC-038 | HTTPS 无重定向 | DNS/TLS/443 已完成 | 从外网执行 GET/HEAD/Range | 可信证书，最终 URL 与请求 URL 相同，无 30x，头/字节与 loopback 一致 | P0 | 待外部条件 |
| TC-039 | 80/443 和公网可达 | 域名解析与安全组已完成 | 多个外部网络访问 | 443 可达且只暴露媒体 prefix；控制 API、公用目录和其他路径不可达 | P0 | 待外部条件 |
| TC-040 | TikTok URL Property | App 负责人完成验证 | 核对 Developer Portal 证据与精确 origin/prefix | 生产 URL 落在已验证 property 下；证据归档 | P0 | 待外部条件 |
| TC-041 | 34.8 分钟关闭态实片 | 生产素材 4665764，三门禁为 0 | local prepare；外网首/中/尾 Range 与 ffprobe | 低于 500 MB、既有 720×1280 HEVC/音频/时长合同成立；无 COS 上传、无 TikTok init | P0 | 待执行 |
| TC-042 | COS 关闭态回滚 | local 外网 canary 失败且有旧 local pending | 保留原 local 只读配置和 key，后端切回 COS并重启，创建新 job | 新 job 使用 COS；旧 local URL 仍可读、文件/账本不误删、不发布 | P0 | loopback 自动化通过；生产待执行 |
| TC-043 | verified origin 防误开 | 三个原门禁均为 1；分别让 verified origin 为空、为旧 COS origin、为当前 local origin | 检查 health 并调用纯 Mock publish | 前两种 `ready=false` 且 init=0；只有与当前后端规范化 origin 精确一致时该附加条件通过 | P0 | 自动化通过 |
| TC-044 | prepared URL origin 逐次复验 | 配置门禁 ready，但 ready manifest URL 被改为不同 HTTPS origin | 调用纯 Mock publish | 返回 `tt_publish_url_property_mismatch`，TikTok init=0；相同 origin 时才继续后续门禁 | P0 | 自动化通过 |
| TC-045 | init HTTP 结果禁止误清理 | TikTok init 分别返回 500/408/409/425/429 和业务拒绝 | 检查 ledger 并跨过清理宽限运行扫描 | 前五类全部为 `init_outcome_unknown`；`init_rejected` 与 unknown 首版均保留媒体且不重试 init | P0 | 自动化通过 |

## 回归范围

- GPU 既有下载、剪辑、Logo、Drama ID、phone-match、固定片尾、720P HEVC
  profile、H.264 回退、SHA/大小/时长和 4 GiB 硬上限。
- CPU prepare 的 `expected_profile` 握手、ready 复用、素材池、每日 FIFO、
  手动立即发布、claim/reconcile 和 unknown 禁重发。
- 三项发布门禁关闭时“不消费素材”的强合同。
- TT Token 不落盘、控制端口 loopback、CPU↔GPU 反向隧道与现有服务健康。
- X 素材 140 秒上限、X SQLite、X runner/timer、X 历史 ledger 与链接均
  不受影响。
- COS 单请求超时、有界 multipart、complete unknown 恢复和旧 URL 精确
  复验。
- 生产验证必须记录 release SHA、配置后端、文件 SHA/大小、外网命令结果、
  证书链、DNS、端口、URL Property 证据和 TikTok init 调用数 0。
