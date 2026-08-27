# SA 需求评审（2026-08-27 现行）

用户最新确认全部业务查询归 CPU。CPU 新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 用本地固定 SHA manifest 替代 GPU 目录查询；独立 204/204 回归、CPU 3.9.6 实际文件/原函数验证 PASS。页面与任务协调端不依赖 GPU 查询目录，也无需媒体素材包；HK 仅完整参数制作、上传 COS 并回传。详见 [职责边界](cpu-gpu-boundary-20260827.md)。

HK 独立 release `e1f5a1d04cfb510df9c2444ac592adec2827508b` 的 v3 auto/manual、重启复用和独立报告/8 PNG 验收仍有效，本次未重跑或改动 HK。此前 188 项及 c719 的 166 项为历史批次，不与新 204 项相加；旧三表演练只绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，不能改绑新 CPU 候选。

整体正式发布仍 HOLD：生产合法 admin/migrator/writer 权限尚未具备，HK 仍使用 canary COS 前缀。用户已授权环境完成后继续部署，并仅指定 Shahrul Ikmal 做一次内部 unlisted 视频及一条评论；不授权 public 测试。全部支持操作仅 SSH，禁止腾讯云管理后台。CPU 主应用未部署/重启、未切流，仍保留 legacy 18787；现状以 [部署状态页](deployment-status-20260827.md)、[测试报告](test-report.md) 与 [HK 实测记录](hk-gpu-setup-20260827.md) 为准。

## 架构决策

- CPU 是全部业务查询、模板选择/配方冻结、任务、OAuth、发布、审计和同步账本唯一协调端；HK 是无业务数据库/YouTube 凭据的媒体执行端，只校验本地制作资源和处理完整媒体参数。CPU 目录缺配置/漂移失败关闭，不向 GPU fallback。
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
- HK e1f5 的 v3 auto + manual 共 4 输出、完整下载解码及封面 callback PASS；两个随机输出均 720×1280/High/5 秒/150 帧。fresh 79.44 秒，真实重启 worker+tunnel 后、fixture HTTP 服务未运行时，两 job 重复 POST 均 200，manifest SHA/mtime 不变、workdir 无重建，replay 1.710 秒、Result=success。报告与 8 PNG 的独立只读复核不另算真机执行。
- HK COS 仍为 `drama-synthesis-canary/20260827`。正式激活须先独立备份配置、切 production prefix 后验收；不得因 dark/canary 测试通过而宣布正式发布，也不得重启/切换现有 X 或 ads 服务。
- 固定 public 的正式功能合规风险独立保留；一次 unlisted 测试授权和代码 QA 均不代表该风险已接受或正式 live/sync 可以开启。
- 真正 YouTube 上传/评论当前均为 0；指定一次测试已有授权，但必须先解除上述技术/权限门禁。HK 始终不接收 YouTube/OAuth/数据库发布凭据。

## 历史边界

2026-08-26 Wave8 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883`、后续旧候选及 2026-08-27 c719 的 166 例属于历史批次，不能叠加到当前 188 项。c719 的媒体幂等失败已在 e1f5 闭环，但 CPU 三表证据不得随之改绑。旧“真实 canary 尚未授权”或“缓存仍待部署复验”不能作为当前结论；当前待补的是合法生产权限/凭据和正式激活门禁。
