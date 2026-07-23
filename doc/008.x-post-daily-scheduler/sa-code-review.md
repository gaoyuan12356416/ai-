# SA 代码评审

## 结论

滚动独立评审累计发现 32 个 P1；均已修复并补回归。最终独立复审结论 GO，未发现残余 P0/P1；当前只剩生产 systemd/migration/权限门禁，部署前未发布新的真实 Post。

## 评审范围

- `features/x_posts/{service,selector}.py`
- `features/x_accounts/{oauth_service,client}.py`
- `scripts/x_post_daily_runner.py`
- systemd/env/Nginx、后台 API/页面、SQLite 迁移及旧 canary 回归

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| SA-001 | P1 | `selector.py` | `_` 被正则视为 word char，危险标签可漏拦 | 统一分隔符后匹配并加 5 个回归样本 | 已修复 |
| SA-002 | P1 | `service.py` | 缺失合规字段被默认成 0 | 五项证据必须显式、非 NULL、无冲突且为 0 | 已修复 |
| SA-003 | P1 | `x_post_daily_runner.py` | 结构化 known 5xx 被误判 unknown | plan/publish 由 Sidecar 显式返回 outcome marker，调用方不靠状态码猜测 | 已修复 |
| SA-004 | P1 | `oauth_service.py` | published 重放仍先校验 Token；前置账号失败不落日志 | 先读/预留 ledger；published 直接返回，前置失败落 known failure | 已修复 |
| SA-005 | P1 | `service.py` | 明确 Create Post 429 被 `post_creating` 改成 unknown | handled error 只信调用方 unknown；崩溃残留独立处理 | 已修复 |
| SA-006 | P1 | `service.py` | 新 run_date 幂等键破坏现网 canary 重放 | 保留 source_date 旧键，新增派生列只在显式提交时比较 | 已修复 |
| SA-007 | P1 | systemd/storage | 未挂载/不可写时可能先建计划 | mount、固定路径、空间、原子写门禁必须在计划前 | 已修复 |
| SA-008 | P1 | ffprobe/systemd | ffprobe 继承密钥且服务以 root 解析外部媒体 | 最小子进程 env、DEVNULL、专用低权用户、`/opt` release、遮蔽密钥 | 已修复 |
| SA-009 | P1 | runner workdir | 默认 `/tmp` 可能让 3×512MiB 预检占满根盘 | 固定数据盘 `daily-work`，unit 只开放该写目录 | 已修复 |
| SA-010 | P2 | media/short/schema/UI | 可变媒体 TOCTOU、短链主机宽松、legacy FK、stopped 展示缺失 | 冻结/复核 SHA-256+尺寸；固定短链；触发器补完整性；补状态 | 已修复 |
| SA-011 | P1 | `publish_canary` / queue wrapper | 短链准备或冻结用户名不一致可能留下永久 `reserved` | 准备阶段纳入统一异常边界；仅对仍为 `reserved` 的日志原子写 known failure | 已修复 |
| SA-012 | P1 | X HTTP / runner response | Create Post 截断响应被当确定失败；HTTP 200 空对象被当 published | 捕获 `HTTPException/IncompleteRead` 为 unknown；严格绑定 status/log/post/preview/short URL | 已修复 |
| SA-013 | P1 | media preflight | 失败候选文件到整池结束才清理，理论峰值接近 25 GiB | 每个候选 `finally` 立即 unlink，高水位固定为一个素材 | 已修复 |
| SA-014 | P1 | storage point-of-use | 初始 preflight 后挂载可消失并写入根盘底层目录 | plan 提交前、短链原子替换前、每次 X 写入前复检 mount/device；生产路径禁止自动 mkdir | 已修复 |
| SA-015 | P1 | runner Sidecar HTTP | Sidecar 截断/超大/畸形发布响应可能被当普通失败 | 捕获 `HTTPException`；发布 POST 发出后的读取/大小/结构错误一律 unknown | 已修复 |
| SA-016 | P1 | final ledger commit | X 已返回 Post ID 后，`mark_published` 失败被写成 known failure | 固化 Post ID/URL 证据并标记 unknown/needs_review；禁止再次发帖 | 已修复 |
| SA-017 | P1 | production directory bootstrap | 新部署未预建 `s2l`/`media-work`，会在计划或首发失败 | 部署显式创建/chown/mode；preflight 同时要求 `media-work` 位于相同数据盘 | 已修复 |
| SA-018 | P1 | runner HTTPError body | HTTP 错误体读取截断会绕过 sibling exception handler | 在 `HTTPError` 分支内部捕获 `HTTPException` 并继承写后 unknown | 已修复 |
| SA-019 | P1 | streamed media | 素材响应流截断会中止整批而非补位 | 下载层转 stable known error；候选层捕获 HTTPException 并继续下一素材 | 已修复 |
| SA-020 | P1 | known error taxonomy | Create Post 前的媒体处理失败/超时/响应过大被误判 unknown | Sidecar 对明确 pre-create error 返回 `outcome_known=true` 并回归 | 已修复 |
| SA-021 | P1 | dangerous tags | 常见复数/派生词 `weapons/nudes/murders/suicidal` 漏拦 | 扩展英文词形且做 selector 端到端排除 | 已修复 |
| SA-022 | P1 | daily-plan payload | 三条合法非 ASCII 描述可超过 Sidecar 16KiB 上限 | 描述统一 4096 字符；daily-plan 独立 256KiB 硬上限 | 已修复 |
| SA-023 | P1 | publish HTTPError | 畸形/未知 409 或无 marker 错误可能被当 known | 写请求错误必须显式 outcome marker；缺失/畸形一律 unknown | 已修复 |
| SA-024 | P1 | daily bearer 权限 | runner 复用广权内部 token，可调用 canary/任意账号/账户管理 | 独立 daily token、固定路由和三账号 allowlist，仅正式 run queue 可 publish | 已修复 |
| SA-025 | P1 | short redirect durability | chmod 后未 fsync inode、replace 后未 fsync 目录 | fd 上 fchmod+fsync，replace 后目录 fsync；重放同样同步 | 已修复 |
| SA-026 | P1 | plan response identity | 重复 queue ID/账号或错误日期可被接受并虚报 3/3 | 严格校验三条唯一正整数 ID、账号顺序和 run/source date | 已修复 |
| SA-027 | P1 | Create Post non-JSON 5xx | 非空畸形 500/503 被记 known，可能重发 | Create Post 非 JSON 5xx 一律 unknown/needs_review | 已修复 |
| SA-028 | P1 | daily-plan outcome | 明确事务回滚 409 缺 outcome marker，被当 unknown 且不留 failed_preflight | plan 结构化错误显式 `outcome_known=true`；通道/畸形仍 unknown | 已修复 |
| SA-029 | P1 | verify/application rate limit | 发布前二次 verify 429 被降为 502 并继续；应用级 usage cap 未识别 | HTTP 429 + 官方 problem type/code 88 统一稳定 429，run stopped | 已修复 |
| SA-030 | P1 | dangerous taxonomy | gun/shooting/torture/R18 与常见中文标签漏拦 | 扩充中英明确色情/暴力 taxonomy，并做参数化/端到端回归 | 已修复 |
| SA-031 | P1 | systemd env privilege | 非 root Sidecar 启动时重读 root:0600 env 导致 EACCES | systemd 注入后 PermissionError 安全跳过，缺配置仍由启动门禁阻断 | 已修复 |
| SA-032 | P1 | loopback proxy | bearer/readiness 客户端可能受 http_proxy 影响而泄露或被伪造 | 三个 loopback opener 显式 `ProxyHandler({})`，公网 X opener 不变 | 已修复 |

## 编译 / 验证结果

- 125 项离线测试全通过；最终独立静态复审 GO。
- 已验证：legacy published canary 跨日重放零 X 请求；明确 429 为 known/stopped；known 502 继续其余账号；unknown 停批。
- 已验证：数据盘 point-of-use preflight、媒体指纹变化拒绝、逐候选清理、ffprobe env 不含 X/MySQL 密钥。
- 已验证：短链失败/冻结用户名变化不留 `reserved`；Create Post 截断与 HTTP 200 空响应均进入 unknown。
- 已验证：Sidecar 截断/超大/畸形响应均 unknown；最终 ledger 写失败保留 Post ID/URL 并进入 needs_review。
- 已验证：daily bearer 路由/三账号范围、plan/publish outcome marker、短链目录持久化、官方限流 taxonomy、root-only env 启动和 loopback 禁代理。
- `py_compile`、Python 3.9 grammar、JS/JSON syntax 与 `git diff --check` 均通过；生产 Python 3.9 编译仍在服务器门禁复跑。
