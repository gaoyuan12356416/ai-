# 当前部署所需的数据库授权

## 当前问题

CPU 的 SSH 可以正常使用，环境搭建和三表备份/本机恢复演练已由代理执行。阻塞不是 Linux 权限，而是目标 MySQL 的账号权限：已有 `ads_aius@43.166.187.96` 对 `kunlunads_dev` 只有 SELECT/SHOW VIEW，不能添加本方案需要的三个字段/索引，也不能为发布结果写入三张表。其他 schema 上的授权不能转用。

尚未在生产 MySQL 执行 DDL、安装 writer/RPC 或向指定频道上传测试视频。用户要求仅通过 SSH 处理；不进入腾讯云管理后台，不重置现有账号密码，不借用无关账号。

## 需要提供的支持

只需二选一，由具备合法数据库授权的人处理：

1. 将有建账号和授权能力的目标 MySQL 管理连接配置安全地放到 CPU 服务器的 root-owned 0600 文件中，并告知代理该文件的绝对路径。不要在聊天、Git、命令行或日志中发送密码。代理再通过 SSH 完成限定账号、迁移、服务部署和验证。
2. 由数据库管理员直接创建下面两个受限账号，把各自连接配置保存在服务器安全文件中，再告知路径；代理处理其余工作。

| 账号（连接来源固定 CPU 43.166.187.96） | 逐表权限 | 生命周期 |
| --- | --- | --- |
| drama_youtube_migrator | SELECT, INSERT, CREATE, ALTER | 一次性迁移；完成后撤销并销毁 |
| drama_youtube_writer | SELECT, INSERT, UPDATE | 长期运行；不持有 DDL |

精确表范围仅为 `kunlunads_dev.ads_youtube_videos`、`kunlunads_dev.ads_youtube_comments`、`kunlunads_dev.ads_youtube_publish_log`。禁止 schema wildcard、DELETE、DROP、INDEX、GRANT OPTION 及多余 routine/proxy 权限。目标迁移/写入端点固定 `101.32.56.53:63353`；实际连接时还必须确认主库可写及账号/授权读回一致。

迁移和 writer 的专用 JSON 合同为 `host`、`port`、`user`、`password`、`database` 五个键；root 管理凭据不复制到运行服务。最终 migrator 文件为 root:root 0600；writer 文件为 drama-youtube:drama-youtube 0600，由对应服务身份读取。代理负责精确路径、owner、权限和 SQL 预检，用户无需手工配置 RPC 或服务。

## 权限具备后继续执行

先验证备份证据仍在时效内且匹配 CPU 候选 SHA；过期则重新做受控演练，不改时间戳伪造。然后按 [migration.md](migration.md) 和 [deploy.md](deploy.md) 执行生产迁移、最小权限 writer/鉴权 RPC、CPU 备份部署及香港 GPU 切换。最后仅用已授权的 Shahrul Ikmal 做一次内部 unlisted 上传、一条评论及三表读回；全部通过后才完成正式功能放行。
