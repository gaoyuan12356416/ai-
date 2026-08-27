# 当前部署所需的数据库授权

## 当前结论（2026-08-27 16:06，北京时间）

新三表已建成，原表零写。当前阻断是**创建专用数据库账号的权限不足**，不是 `ads_ai` 无写权限，也不是等待 `kunlunads_dev` 迁移。以下现场结果由根代理通过 CPU SSH 核验，本页仅记录交接。

- 候选 `6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f` 已 GitHub/readback、CPU clean；16:00 全新隔离 MySQL 5.7.44 演练 9 checks PASS。
- 16:01:35 在生产写端 `101.32.56.53:63353` 仅 CREATE `ads_ai.ads_youtube_videos`、`ads_ai.ads_youtube_comments`、`ads_ai.ads_youtube_publish_log`；三表完整兼容，apply 后管理员验证无 trigger/FK。16:06 经正常读端 63350 核验各 0 行，未复制或写入原表。
- 16:04:58，实际 `ads_aius@43.166.187.96` 执行一次限定新 writer 的精确 GRANT，返回 MySQL 1410：`You are not allowed to create a user with GRANT`；`global CREATE_USER=false`。隔离 MySQL 5.7.44 中，schema ALL + GRANT OPTION 账号同样复现 1410。
- `ads_aius` 的 `ads_ai.* ALL PRIVILEGES WITH GRANT OPTION` 足以完成本次新表 bootstrap，但不能据此认定能隐式创建账号。此前“可直接完成最小 writer 配置”的前提遗漏已登记为 [BUG-027](bugs/BUG-027.md)，属于部署权限前提缺漏，非应用代码问题。
- 未将广权限管理凭据配置给 runtime；新 writer/RPC 未安装、未启动；真实 YouTube 上传 0、评论 0。现有生产应用及 HK 未因本次阻断切换。

## 现在需要提供的支持

由具备合法建账号及授权能力的数据库管理员，把管理连接配置放在 **CPU 服务器 root-owned 0600 文件**中，只提供其**绝对路径**。不要在聊天、Git、命令行或日志中发送密码。后续仍仅通过 SSH，不进入云控制台，不借用无关账号。

代理使用该管理连接先只读核实目标端点/身份及精确账号是否存在，再由合法 DB admin **显式 CREATE USER，然后逐表 GRANT**：

| 运行账号 | 唯一表范围 | 允许权限 |
| --- | --- | --- |
| `drama_youtube_writer@43.166.187.96` | `ads_ai.ads_youtube_videos` | `SELECT, INSERT, UPDATE` |
| 同上 | `ads_ai.ads_youtube_comments` | `SELECT, INSERT, UPDATE` |
| 同上 | `ads_ai.ads_youtube_publish_log` | `SELECT, INSERT, UPDATE` |

如果精确账号已存在，立即停止并核实来源、用途及授权；不得 ALTER、重置密码、覆盖或默默复用。运行账号不得有 schema wildcard、DDL、DELETE、GRANT OPTION 或额外 routine/proxy 权限，不得使用 `ads_aius` 作为 runtime 账号。管理凭据不复制到运行服务。

writer 连接 JSON 使用 `host`、`port`、`user`、`password`、`database` 五个键，固定写端 `101.32.56.53:63353`、schema `ads_ai`；运行文件由 `drama-youtube:drama-youtube` 持有、0600。账号建立后先核验实际身份、最小授权及 `drama-youtube-writer-preflight-v2` 健康合同，再由根代理继续服务部署和已授权的单次 canary。新表 bootstrap 已完成，不再次以建表证明代替账号权限验证。

不再需要旧库迁移、旧表备份或旧 migrator。保留已创建的新表及现场证据，不 DROP/ALTER/DELETE，不为解除阻断改写原表。现行合同见 [新表合同](ads-ai-new-tables-20260827.md)，整体上线仍 HOLD，见 [部署状态](deployment-status-20260827.md)。

## 历史记录：旧 kunlunads_dev 范围（已退休，不可执行）

以下保留当时诊断及方案来龙去脉；其中“未执行 DDL”、旧表权限、备份、迁移及两个账号的要求均不是当前状态或继续条件。用户已明确只在 `ads_ai` 新建表，以下历史文字不授权任何原库写入。

### 历史问题

CPU 的 SSH 可以正常使用，环境搭建和三表备份/本机恢复演练已由代理执行。阻塞不是 Linux 权限，而是目标 MySQL 的账号权限：已有 `ads_aius@43.166.187.96` 对 `kunlunads_dev` 只有 SELECT/SHOW VIEW，不能添加本方案需要的三个字段/索引，也不能为发布结果写入三张表。其他 schema 上的授权不能转用。

尚未在生产 MySQL 执行 DDL、安装 writer/RPC 或向指定频道上传测试视频。用户要求仅通过 SSH 处理；不进入腾讯云管理后台，不重置现有账号密码，不借用无关账号。

### 历史支持方案（已退休）

只需二选一，由具备合法数据库授权的人处理：

1. 将有建账号和授权能力的目标 MySQL 管理连接配置安全地放到 CPU 服务器的 root-owned 0600 文件中，并告知代理该文件的绝对路径。不要在聊天、Git、命令行或日志中发送密码。代理再通过 SSH 完成限定账号、迁移、服务部署和验证。
2. 由数据库管理员直接创建下面两个受限账号，把各自连接配置保存在服务器安全文件中，再告知路径；代理处理其余工作。

| 账号（连接来源固定 CPU 43.166.187.96） | 逐表权限 | 生命周期 |
| --- | --- | --- |
| drama_youtube_migrator | SELECT, INSERT, CREATE, ALTER | 一次性迁移；完成后撤销并销毁 |
| drama_youtube_writer | SELECT, INSERT, UPDATE | 长期运行；不持有 DDL |

精确表范围仅为 `kunlunads_dev.ads_youtube_videos`、`kunlunads_dev.ads_youtube_comments`、`kunlunads_dev.ads_youtube_publish_log`。禁止 schema wildcard、DELETE、DROP、INDEX、GRANT OPTION 及多余 routine/proxy 权限。目标迁移/写入端点固定 `101.32.56.53:63353`；实际连接时还必须确认主库可写及账号/授权读回一致。

迁移和 writer 的专用 JSON 合同为 `host`、`port`、`user`、`password`、`database` 五个键；root 管理凭据不复制到运行服务。最终 migrator 文件为 root:root 0600；writer 文件为 drama-youtube:drama-youtube 0600，由对应服务身份读取。代理负责精确路径、owner、权限和 SQL 预检，用户无需手工配置 RPC 或服务。

### 历史继续步骤（已退休）

先验证备份证据仍在时效内且匹配 CPU 候选 SHA；过期则重新做受控演练，不改时间戳伪造。然后按 [migration.md](migration.md) 和 [deploy.md](deploy.md) 执行生产迁移、最小权限 writer/鉴权 RPC、CPU 备份部署及香港 GPU 切换。最后仅用已授权的 Shahrul Ikmal 做一次内部 unlisted 上传、一条评论及三表读回；全部通过后才完成正式功能放行。
