# 2026-08-27 最新确认：现有授权发布，ads_ai 新三表记录

执行结果（17:50）：本方案已部署并放行，正式live/sync=1；指定Shahrul Ikmal唯一非公开视频及一条评论成功，新三表各1、3outbox已同步、完成后同ID幂等和原授权字段守恒通过。组件版本、完整证据、测试边界与回滚见[上线验收记录](release-acceptance-20260827.md)。无需专用账号或额外管理员支持，不重跑已完成DDL。

## 现行范围

用户最新明确要求“通过已经获取到 token 支持完成发布并做记录，不用考虑专用账号隔离也不要动原表”。本文件取代旧版三表迁移、专用数据库账号和 c719 恢复演练门禁；附件其他要求及已验收 UI 不变。所有操作仅 SSH，不进入腾讯云管理后台。

- 仅新建 `ads_ai.ads_youtube_videos`、`ads_ai.ads_youtube_comments`、`ads_ai.ads_youtube_publish_log`，只保存本功能新产生的事实，不复制、更新或变更原表。
- `kunlunads_dev` 的频道和 OAuth 授权继续只读查询；不全局切换 `DB_NAME` 为 ads_ai。新记录不会自动出现在仅查询旧三表的其他后台页面，不为此暗中双写。
- CPU 完成业务查询、SQLite、短链、发布、评论和新表写入；香港 GPU 只制作、上传 COS、返回结果。
- 现有 `gy.g2flow.com/s2l/youtube/<数字>.html`、落地页参数合同、唯一一次 Shahrul Ikmal unlisted 测试及一条评论均不变。

## 数据与写入边界

固定 DDL 为 `deploy/drama-youtube-ads-ai-v2.sql`。三表均 InnoDB、utf8mb4_bin，并有所有权 COMMENT `drama-synthesis:youtube-ledger:ads_ai:v2`；无触发器、无外键。视频与日志存完整发布 payload，评论存完整评论 payload，另存不可变 payload_json/SHA256/canary_operation_id。URL、描述、评论不做旧表长度截断，不构造负数旧队列 ID。publish_id 和外部 video/comment ID 的精确唯一键保证重复调用只复用相同内容；内容不一致拒绝，日志不依赖 outbox 写入顺序。

操作者 ID 为 VARCHAR(128) 的原始飞书字符串，普通历史未知可以为空，拒绝控制字符且不归零/截断；canary 的 CLI、store/claim 和 v2 payload 都必须非空安全 ID。固定 DDL SHA256 为 `08efc2e9d7e7bb52eb9bf041e9133acb214ca6dc8b8c7d86cb73d6d80ee8be38`。

运行时改用 CPU 已有 `ads_aius@43.166.187.96` 数据库凭据，不创建或授权新账号。应用仅允许这三张新表 SELECT/INSERT/UPDATE，不提供任意 SQL、DDL、DELETE、原库写入、令牌更新或历史数据导入入口。已有账号在数据库层权限较广，这是用户明确接受的边界，不能宣称数据库最小权限隔离。

RPC 仍为 CPU loopback 18837（18836 属于 FB），health 必须为 `drama-youtube-writer-preflight-v3`、schema ads_ai、精确实际身份，且 `credential_mode=shared-existing-account`、`write_boundary=application-table-allowlist`、`db_least_privilege=false`。每次操作检查三表结构/唯一索引及有效 SELECT/INSERT/UPDATE 权限；有界元数据查询只针对当前 grantee、ads_ai 和精确三表。先证明 TRIGGER 可见性，再证明无 trigger/FK，防止经新表间接改写原表。v1/v2 或伪称隔离的响应不能通过。

YouTube 使用已有频道 OAuth 授权；需要刷新 access token 时仅在 CPU 内存使用，不向原频道、账户、token 或旧发布表写入。密钥不进入日志、聊天、Git 或 GPU。本次取消的是数据库专用账号，不取消 RPC 鉴权、OS 服务用户和私有文件权限。

## 建表与账号流程

1. 只读发现走 CPU→101.32.56.53:63350。2026-08-27 15:04 实查 ads_ai 无 YouTube 同名表/视图；ads_aius 对 ads_ai.* 为 ALL PRIVILEGES WITH GRANT OPTION，对原库仍只读。
2. 新候选必须先本地测试、独立复审、commit/push，再在 CPU 从 GitHub 拉精确 clean SHA。生产三表已在 16:01:35 创建成功；`scripts/bootstrap_drama_youtube_ads_ai.py` 与固定 DDL 的既有证据保留，本次不再运行 DDL，不创建新表以外的对象。
3. 先运行 dry-run。先检查全部三个对象；仅允许创建缺失表，已存在表须完整兼容，任一不兼容立即停止，不 ALTER/DROP/DELETE/REPLACE、不清理失败现场。
4. CPU 全新隔离 MySQL 5.7.44 演练：127.0.0.1:23358、context `2026082715040001`、目录 `/mnt/data-disk/drama-youtube-ads-ai-rehearsal-2026082715040001`，无原数据。固定 CREATE、重复执行、精确 payload/冲突和证据 SHA 必须通过。旧 23357/c719 恢复环境只保留为历史，不改绑。
5. `--apply` 仅允许批准的 ads_ai 三表写入口 63353，绑定候选及新演练证据。63353 不能用于普通查询、扫描、联表或报表，也不改变通用 MYSQL/ADMIN_MAPPING 读端口。apply 先验证固定身份、目标、可写状态与全部对象，完成后再核对结构及无 trigger/FK。
6. 按最新用户决定，直接复制 CPU 私有 `admin-write-db.json` 的五个现有连接字段到受保护 runtime 文件，固定 101.32.56.53:63353、ads_aius、ads_ai；不修改原连接文件，不 CREATE USER/GRANT/ALTER USER，不需要新增管理员支持。此前 1410 是已被需求变更取代的账号门禁，不是权限已被修复；旧 `writer-account-pending.json` 不可作为运行凭据。
7. 秘密只在 CPU 私有文件。客户端 token root:root 0600，服务端同值 token 和 writer DB JSON 为 drama-youtube:drama-youtube 0600；不打印原值、不通过命令行传密码。writer 在 `/opt/drama-youtube-unified-writer/releases/<candidate_git_sha>` 安装、current 指向精确候选，以实际用户预检后才启动服务。

旧 `migrate_drama_youtube_unified_schema.py` 与 `drama_youtube_three_table_rehearsal.py` 已退出执行路线；保留历史证据不代表允许运行旧库写入。

## 应用发布、测试与回滚

- 先保存 CPU app/static/feature 文件及 env/systemd 元数据、SQLite 一致性备份和现有队列；无活动制作后才切流。只覆盖本需求文件，保留 X/FB/TT 其他改动。
- CPU 还须从同一候选补齐未安装的 `features/fb_gpu/{__init__,prepare_worker,random_overlay}.py` 纯导入依赖，不启动 FB worker；发布器需要 `/usr/bin/ffprobe` 元数据工具，实机缺失时经 SSH 安装并保存版本/散列证明。制作仍由 HK 执行。
- CPU 专用 `/etc/drama-synthesis/cpu.env` 配合 `deploy/95-drama-synthesis.conf`，作为 API/job worker 最后一个 env 文件；新 YouTube worker 同样加载它。原 env/drop-in 不覆盖。目录 manifest 仅在 CPU，HK 不接业务凭据。
- HK 保留已验证的 e1f5a1d 媒体代码；idle 后备份 env，将 COS_PREFIX 与 DRAMA_PUBLIC_BASE_URL 配套激活为现有 drama-materials 前缀。仅重启新增 HK worker，保留旧 X/ads 服务和 18787。
- 新 RPC v3、SQLite 输出归一、gy owner/ACL/Nginx、API/任务 worker、18788 health 全通过后，才按既定唯一 operation 执行 Shahrul Ikmal canary。正式 live/sync 两个开关在测试期间保持 0。原 MySQL 表不动；附件已批准的 CPU SQLite 增量建账和历史输出布尔归一仍执行，不迁移路径、不覆盖历史任务。
- 测试必须确认 processed/succeeded/unlisted、恰好一条真实评论、新三表各一条、重复执行无重复；submitted/processing/单有 video_id 不算完成。未知结果先对账，不创建替代视频/评论。
- 回滚先关 live/sync 并停止新 claim，确认没有 in-flight/unknown 后回退此次应用文件和专用 drop-in、切回旧 18787。新三表、SQLite/outbox、短链、COS、视频/评论全部保留；不 DROP、删行或反向 DDL，也不恢复旧库覆盖无关数据。

## 当前执行状态（16:35，后续实测见部署状态页）

现有账号实现已完成首轮 107/107 专项测试，独立冻结回归待执行；不与历史 262/23 项相加。16:30 CPU 上线前备份已完成，目录 `/mnt/data-disk/drama-synthesis-cpu/backups/20260827T1630-pre-shared-account`，manifest SHA256 `4376493cd308ecf10d61442468f3d7df0ef76a586cc336cb6c3aa0c8fda306da`，SQLite 一致性备份 SHA256 `38a78542dd8f3f481da1f28b8a5de01c5a051e8947b3aac486223e8317d8c06f`。原应用/配置尚未切换；20 done、无活动制作。CPU 18788 media-only health 与 gy owner/ACL 检查通过。以下历史账号阻断不再是当前待办。

## 历史证据状态（保留时间与原候选，不作为当前账号门禁）

16:06 当前检查点：生产三表已经在 16:01:35 创建成功，结构/唯一索引/所有权均兼容；apply 后由管理员验证无 trigger/FK，随后 63350 回读每表 0 行。生产 writer 账号创建被 1410 拒绝，未安装/启动 RPC、未配置广权限 runtime。已检查 CPU 标准 MySQL 管理配置（含 include 目录），未发现可用 CynosDB 管理凭据；不是 ads_ai 缺少建表或写入权限。下一步需要合法管理员将连接配置存为 CPU root:root 0600 文件并告知绝对路径，之后由代理继续 SSH 操作。

当前 CPU app SHA `a956fb9952aa09d8d911cf3a5c54b58525cb81935d92d0ede698af9c681675a3` 未变；API PID 3841722、job worker PID 1212 均 active/NRestarts=0；20 done，SQLite quick_check=ok，尚无 YouTube 本地账本。未切 UI/18788、未改 HK 正式前缀，真实上传/评论仍为 0。CPU 已安装经版本/散列验证的 ffprobe，`/usr/bin/ffprobe` 指向数据盘独立版本，未覆盖既有二进制。

本次所有实机发现/演练/建表报告绑定精确候选 `6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f`，私有证据目录 `/mnt/data-disk/drama-youtube-ads-ai-deploy-20260827/6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f`：

| 证据 | SHA256 |
| --- | --- |
| discovery.json | c6ddd65481ba737d3df6ab52518f54ec04bddb823004c1594ed8e5e8b7c8ad69 |
| fresh-rehearsal.json（9 项全部通过） | d3a5bbee26768b1c2a2cce266c713b9780bc40824c0375009f1f662d6c1b8db4 |
| bootstrap-evidence.json | 786937167aae1d2f9bf1c9867866665ce433f66b7d7038b3a07b724c5bb7e2b5 |
| production-create.json | f4d99218edd71f75c148d591db05d6a3967d41571978b6a7bf824d9d35fd36fb |
| checkpoint-readback.json | 816114abbd3ebb5a88fd437a75884940b08de86d8a26ae43e551334e1f89a111 |
| writer-account-provision.json | 95f1e45b2d8296eea450e257bf6c21c1cf57a78cefd5d196820cec553c9cc4c5 |

writer-account-provision.json 记录真实 1410 和权限检查，密码不在证据或日志中。root 私有 writer-account-pending.json 只记录尚未成功建立的账号意图，不能当成有效 runtime 凭据。ffprobe n7.1-152-gd72536008a-20250113 SHA256 为 `bf7b813bb81f01695a38841e697d6fd858c194baf13017e78c2855af502e644a`，路径 `/mnt/data-disk/drama-synthesis-cpu/runtime/ffprobe-n7.1-20250113/ffprobe`。16:09 已核验容器 ID/标签/数据路径后停止此次隔离 MySQL 容器，数据和报告保留，不占用运行资源。生产新表保持保留，不 DROP 或写探针；不把建表完成当成全功能上线。

### 较早检查点（保留，不覆盖上述当前事实）

15:58 增量：f3d754e 已 GitHub push/readback 并在 CPU 精确 clean checkout；生产只读 dry-run PASS，三表均缺失。隔离 MySQL 5.7.44 的库名下划线转义使管理员预检拒绝，尚未创建任何表。BUG-026 两文件兼容修正已独立 23/23 定向测试和 6/6 内存对抗通过，固定 DDL/runtime parser 未变。新候选重新绑定实机发现与演练，原 262/262 仅作为未变代码基线，生产建表和外部发布仍未执行。

15:23 只读核验 CPU 原应用 SHA 未变、20 个任务全部 done、SQLite quick_check=ok；尚无 production YouTube ledger。新隔离 MySQL 5.7.44 已启动，ads_ai 为空，数据盘独立目录/loopback 端口/0600 admin JSON 已确认。生产新表、writer、CPU 切流和外部测试仍待代码冻结及实机门禁；本段不将准备完成记为部署成功。

冻结代码已完成独立 10 模块一次完整 262/262 PASS（14.628 秒，含 X30/TT11）。实现专项39项、root专项及旧204基线不与262相加。浏览器实查旧 UI 仍默认勾选，说明生产尚未换成新候选；当前登录管理员已确认“郜远”。实机阶段将在本候选 GitHub push 后进行，不能以离线通过替代真实 writer/发布结果。
