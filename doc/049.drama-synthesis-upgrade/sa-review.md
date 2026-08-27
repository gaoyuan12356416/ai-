# SA 需求评审（2026-08-27 现行）

候选 `c719bebf72be900ec3853858dc53b36b83beffd2` 已独立代码 QA、GitHub push/readback；合并回归 166/166、25 文件语法/Python 3.9 AST PASS，另五项内存 mock 媒体对抗 PASS，不能叠加为更多用例或视作真实媒体验收。当前整体发布仍 HOLD，并非“仅剩外部门禁”：HK dark 的同 job/payload 第二次 POST 实测重复制作，缓存窄修仅本地完成、尚待独立回归和增量发布复验；生产数据库合法管理/迁移/写入权限也未具备。

用户已授权环境完成后继续部署，并仅指定 Shahrul Ikmal 做一次内部 unlisted 视频及一条评论；不授权 public 测试。全部支持操作仅 SSH，禁止腾讯云管理后台。CPU 主应用未切流，仍保留 legacy 18787；现状及后续实证以 [测试报告](test-report.md) 与 [HK 实测记录](hk-gpu-setup-20260827.md) 为准。

## 架构决策

- CPU 是任务、OAuth、发布、审计和同步账本唯一协调端；HK 是无 YouTube 凭据的媒体执行端。
- 复用 FB v3 immutable manifest/recipe，业务 recipe 额外冻结 source。
- gy 使用独立 `/s2l/youtube/` namespace 和 app-owned root，避免与 X/TT/FB writer 冲突。
- SQLite 保证短链/发布/outbox 幂等；统一表使用严格白名单适配器，缺依赖时 sync fail closed。
- video ID 不构成成功；processing 与 visibility 读回是 published 门槛。
- 正式 HTTP/UI 仍只允许 public，普通 worker/outbox 排除 canary。内部 CLI 冻结单 operation、真实 job source、指定 app/channel/account 及 unlisted；重复执行只能推进精确 task，不产生替代上传或 public 升级。
- canary 在 claim/OAuth refresh/upload 前先验证只读 RPC 健康合同、全量 schema/index 与精确 writer 权限；每个 outbox 实体 claim 前再 fresh 验证同视频 processed/succeeded/unlisted。unknown 或隐私漂移持久 hold，不盲重试评论。
- 正式与 canary 评论共同使用冻结的 channelId/videoId，仅接受相符的真实 topLevelComment.id；HTTP 2xx 本身不是评论成功。

## 风险门禁

- 生产已在现有 `gy.g2flow.com` TLS server 内加入隔离 `/s2l/youtube` location，并建立 app-owned root；未创建域名/DNS/证书，未生成真实短链文件。正式代码切换前仍须校验 writer owner、Nginx 只读与 X 路由不变。
- 三张统一 YouTube 表的真实 READ ONLY 一致性 snapshot 与 CPU loopback MySQL 5.7.44 恢复/迁移演练已 PASS：视频 244151、评论 53、日志 55105，总计 299309 行，证据绑定 CPU 候选 c719beb。精确目录、SHA 与时效见 [migration.md](migration.md)。这不是 Tencent API 备份或全集群灾备验收。
- 生产 `ads_aius` 对 `kunlunads_dev` 仍只有 SELECT/SHOW VIEW；没有合法 admin/migrator/writer，生产 DDL/RPC/真实 canary 因权限硬阻塞。须由合法管理员配置一次性 migrator 的逐表 SELECT/INSERT/CREATE/ALTER，迁移后撤销销毁；长期 writer 仅逐表 SELECT/INSERT/UPDATE、从未持有 DDL。禁止 wildcard、DROP/DELETE/GRANT OPTION 等扩权；`127.0.0.1:18837` health 必须新鲜验证精确合同，18836 保留现有 FB 隧道。
- HK c719beb 已完成真实 auto concat/no-BGM/random 与 COS 三文件下载、5 秒/150 帧/解码检查，但重复 POST 幂等失败，因此不能据此切 CPU 正式流量；完整双模式与修复后的幂等验收仍待主代理完成。
- 固定 public 的正式功能合规风险独立保留；一次 unlisted 测试授权和代码 QA 均不代表该风险已接受或正式 live/sync 可以开启。
- 真正 YouTube 上传/评论当前均为 0；指定一次测试已有授权，但必须先解除上述技术/权限门禁。HK 始终不接收 YouTube/OAuth/数据库发布凭据。

## 历史边界

2026-08-26 Wave8 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 及后续旧候选的架构复审、当时仅 gy 基础配置已部署的状态均是历史记录。旧“仅剩外部门禁”“真实 canary 尚未授权”结论不再适用；旧测试计数不并入 2026-08-27 的 166 例。
