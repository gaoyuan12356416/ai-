# 剧集合成升级部署状态（2026-08-27）

## 最终结论：已部署、已放行、指定测试通过（17:50）

CPU已部署ee6e00c修正，HK正式制作前缀已激活；Shahrul Ikmal唯一canary已完成，视频`HGgjhhRXS-I`、评论`Ugwktiv9_nnXb1TN_c54AaABAg`，上传/评论各1次。17:26新鲜外部读回unlisted/processed/succeeded及全部文案/作者匹配，ads_ai三表各1、3outbox synced，原Token/client指纹不变；17:31完成后同ID重放的4SQLite表/3MySQL表计数与全行hash均不变。无专用数据库账号要求、未写原MySQL表。

最终页面978746f已GitHub精确部署，17:45:45正式live/sync=1，17:49三个进程有效开关/业务读取/GPU地址、稳定运行和canary不重复均通过。BUG-030独立增量及线上加载/完成/评论输入核验通过，无剩余放行阻断。详情、证据、版本和回滚边界以[上线验收记录](release-acceptance-20260827.md)为准。以下为分阶段历史，不得用旧HOLD/管理员账号要求覆盖最新决定或重复执行旧部署。真实外部测试仅为已授权unlisted，不冒称public已实测。

## 历史实机进展：CPU 首次切换与兼容问题发现

已从 GitHub 精确部署 CPU `59f95e6dc106a420fa2e326597c931ba712249f9`；API、原制作 worker、新 YouTube worker、新 writer 均 active/NRestarts=0，正式 live/sync 仍为 0。使用实际 drama-youtube OS 用户的 ads_aius/ads_ai v3 预检与鉴权 RPC 均通过，未鉴权 401；没有新增数据库账号。CPU 业务查询仍 63350/kunlunads_dev，制作明确指向 18788。20 个历史 done 保留，SQLite quick_check=ok；历史输出单事务归一后第二次 dry-run changes=0，新增本地账本为空。

CPU 备份 `/mnt/data-disk/drama-synthesis-cpu/backups/20260827T1630-pre-shared-account`；切流机器报告在 `/mnt/data-disk/drama-youtube-ads-ai-deploy-20260827/59f95e6dc106a420fa2e326597c931ba712249f9/cpu-cutover.json`。旧原页面与仓库基线有素材需求预览差异，但新候选已保留线上原行为；不是未处理的覆盖冲突。

HK 继续 e1f5a1d，已备份并在 16:42 左右将 COS_PREFIX/DRAMA_PUBLIC_BASE_URL 激活为 drama-materials；新增 worker 与其 Requires 隧道 active/NRestarts=0，旧 X 两 PID 91290/91292 未变。备份目录 `/data/drama-synthesis-gpu/backups/20260827T1650-pre-formal-prefix` 的 1650 只是目录标签，不是执行时间，实际时间见 activation.json。既有 ads_video_producer 的独立自重启状态未被本需求改动。

浏览器登录郜远已验证默认四项未选、下拉移除、自动/手动四层 3/5/3/7 目录、多产物选择及“素材 URL 已复制”反馈。任务列表直达三操作遗漏已补入待部署修正（[BUG-029](bugs/BUG-029.md)）。唯一 canary prepare 在凭据资格阶段停止：原 Token 正常，MySQL batch 双重转义导致 scope 误判，见 [BUG-028](bugs/BUG-028.md)。发布行/短链/真实上传/评论均为 0；修正后继续同一 operation，不新建替代测试。以下 16:06 账号阻断是历史，不再适用。

## 最新覆盖决定：使用现有授权与现有数据库账号（2026-08-27 16:35）

用户明确取消专用数据库账号隔离要求。按 [现行发布合同](ads-ai-new-tables-20260827.md) 使用 CPU 现有 ads_aius 和已有 YouTube OAuth，发布结果只写 ads_ai 三张新表；不创建/修改账号、不动原 MySQL 表。RPC v3 如实声明共享账号与应用表白名单，保留秘密保护、无 trigger/FK 检查、幂等与未知结果停止。无需再提供管理员凭据。以下专用账号/1410/旧库迁移内容均是历史，不再作为上线门禁；当前部署及真实测试尚待完成，最终实机状态另行记录。

## 历史快照（16:06）：新表完成，当时建账号权限阻断

整体正式发布仍 HOLD。`ads_ai` 专用三表已创建、原表零写；当前阻断是合法管理员创建专用 DB writer 账号，不是 `ads_ai` 无写权限。以下实机证据由根代理执行并交接；本次文档更新未访问服务器或重新执行部署。

| 时间/组件 | 最新证据与边界 |
| --- | --- |
| CPU 候选 | `6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f` 已 GitHub/readback，CPU checkout clean；不代表生产应用已切换 |
| 16:00 全新演练 | 新隔离 MySQL 5.7.44 fresh-table rehearsal 9 checks PASS；未复制原表数据，不改绑旧 c719 证据 |
| 16:01:35 生产新表 | 63353 仅 CREATE `ads_ai.ads_youtube_videos`、`ads_ai.ads_youtube_comments`、`ads_ai.ads_youtube_publish_log` 成功，三表完整兼容 |
| 16:04:58 账号阻断 | `ads_aius@43.166.187.96` 一次精确新 writer GRANT 返回 1410：`You are not allowed to create a user with GRANT`；`global CREATE_USER=false`。隔离 5.7.44 schema ALL + GRANT OPTION 账号同样复现 |
| 16:06 读回 | 正常读端 63350 三张新表各 0 行；无 trigger/FK 由 apply 后管理员验证；原表零写 |
| CPU 现网 | app 仍 `a956fb...675a3`（缩写），API PID 3841722 / job worker PID 1212；20 done、SQLite `quick_check=ok`、无 YouTube 账本 |
| 尚未执行 | 未配置广权限 runtime 凭据；新 writer/RPC 未安装或启动，CPU 应用未切换；真实 YouTube 上传 0、评论 0；HK 未改 |
| ffprobe | 已新装，SHA256 `bf7b813bb81f01695a38841e697d6fd858c194baf13017e78c2855af502e644a`；`/usr/bin/ffprobe` 指向 `/mnt/data-disk/drama-synthesis-cpu/runtime/ffprobe-n7.1-20250113/ffprobe` |
| 16:09 演练资源 | 本次 23358 隔离 MySQL 容器已核验 ID/标签/数据目录后停止，演练数据与报告保留；未停止生产服务 |

### 已退休的账号阻断条件（不得继续执行）

`ads_ai.* ALL PRIVILEGES WITH GRANT OPTION` 不等于有全局建账号权限；此前遗漏此部署前提，见 [BUG-027](bugs/BUG-027.md)。由合法 DB admin 提供 CPU 上 root-owned 0600 管理连接文件的**绝对路径**即可，不在聊天发密码。通过 SSH 先核实精确账号不存在，再显式 CREATE USER，最后仅向 `drama_youtube_writer@43.166.187.96` 授予上述三表的 `SELECT, INSERT, UPDATE`。账号若已存在即停并核实，不 ALTER/重置/覆盖，不用 `ads_aius` 运行服务，不进云控制台。

不再等待 `kunlunads_dev` 迁移、旧表备份或旧 migrator；已创建新表保持原样，不 DROP/ALTER/DELETE，也不向原表写入。账号可用后由根代理核验实际 runtime 身份、最小权限和 v2/ads_ai 健康合同，按新候选备份、部署并验证 CPU/RPC，最后才执行已授权的 Shahrul Ikmal 单次 unlisted 视频及一条评论。真实 canary 和正式放行尚未完成，不能以建表或离线测试 PASS 代替。详细权限与凭据边界见 [数据库授权说明](db-access-blocker-20260827.md)、[新表合同](ads-ai-new-tables-20260827.md)。

## 历史快照：新表建成前（截至 15:23 及 14:47，不是当前门禁）

下列内容保留旧阶段的证据、版本和操作方案。旧库迁移/备份/migrator 条件已退休，历史“尚未建表”“当前阻塞”“继续步骤”不得作为现行状态或操作授权；当前结论以上文 16:06 为准。

### 历史范围确认：ads_ai 新表

不再写原表，现按 [新表合同](ads-ai-new-tables-20260827.md) 执行。新表写入器/bootstrap 已进入实现和专项测试；全新 CPU 隔离 MySQL5.7.44 运行于 127.0.0.1:23358，未导入旧数据。15:23 核验生产 app SHA 未变、20 done/无活动任务、SQLite quick_check=ok。生产新表/writer/CPU 切流/YouTube 外部测试尚未执行。下文是前一阶段历史，不继续等待旧库写权限。

证据截止北京时间 14:47。用户新增的“查询全部放 CPU、HK 只制作和上传 COS”已落实到代码与隔离验证：CPU 新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc`，独立 204/204 回归、CPU 实机目录读取/原函数验证 PASS。香港 GPU 既有隔离合成验收通过；CPU 正式切换、YouTube 真实测试仍未执行，整体不能标记部署完成。支持操作仅通过 SSH，不进入腾讯云管理后台。

### 历史已完成

- CPU 已安装固定 SHA 的原始 7921-byte 模板 manifest（root:root 0444），没有复制媒体素材包。CPU 模板查询不再调用 GPU；新代码先推送 GitHub，再在数据盘独立 checkout 用 Python 3.9.6 验证 315 组合、auto/manual 冻结、坏配置 503，无网络/数据库/媒体包读取。生产 env 未改、API/job worker PID 与代码 SHA 未变；细节见 [职责边界](cpu-gpu-boundary-20260827.md)。
- 香港 GPU 独立 Python/CUDA/Demucs 四模型、FB-v3 四层 315 组合素材、专用服务用户、worker 与 CPU 18788 隧道搭建完成；不替换系统环境，不修改现有 X/TT/FB/ads_video_producer 服务。
- 自动/手动两种模式共 4 个真实合成产物通过下载、规格和完整解码；两个随机产物均 720×1280、5 秒、150 帧。封面回调通过。即时重复提交和服务重启后重放均复用成片，manifest 指纹/时间未变、工作目录未重建。独立 QA 已复核报告及 8 帧。
- CPU 三张统一 YouTube 表的只读一致性备份、本机隔离 MySQL 5.7.44 恢复、迁移及二次幂等演练通过：244151/53/55105 行，共 299309 行；旧结构/数据指纹保持。演练容器已停止、备份保留。这是三表恢复证据，不是全集群灾备。
- 最终只读检查显示 CPU API/job worker PID、app.py、旧 GPU 地址 18787 未变；SQLite quick_check=ok、20 done、无活动任务。现有 X 短链 200，新 YouTube 未生成路径 404/POST 403。没有真实 YouTube refresh/upload/comment，没有生成真实 YouTube 短链。

### 历史版本与证据

| 组件 | 固定版本/证据 |
| --- | --- |
| HK 当前已运行 | `e1f5a1d04cfb510df9c2444ac592adec2827508b`，GitHub-first、detached clean tree |
| CPU 待部署候选 | `40042f9692fbec58caa5abbf41af35e9aefb54bc`；独立目录/函数验证通过，生产仍为旧应用，不能将候选当成已上线 |
| CPU 三表演练 | 仅绑定 c719beb；`/mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot` |
| HK v3 实测报告 | `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json`；SHA `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175` |
| 最新代码 QA | 七套合并 204/204 PASS，13.639 秒；另 15 项对抗单列 PASS，不叠加；3 Python 文件语法/3.9 AST、4 文件冻结与 diff PASS |
| 历史 HK 代码 QA | 六套 188 项：首次 187 PASS，1 项文档文本合同修复后定向复测 PASS；已包含在最新回归范围内，不与 204/166 相加 |
| HK 切换备份 | `/data/drama-synthesis-gpu/backups/20260827T123135+0800-pre-e1f5a1d` |

最终文档提交不改变上述组件版本。CPU 三表旧证据只绑定 c719beb，不能改绑新 CPU 候选、文档提交或 HK 版本，正式迁移前须按新候选重验/更新新鲜证据。详细测试、产物 SHA、服务读回与回滚分别见 [测试报告](test-report.md)、[HK 记录](hk-gpu-setup-20260827.md)、[迁移](migration.md) 和 [部署](deploy.md)。

### 历史阻塞及支持方案（旧库要求已退休）

CPU 已有账号 `ads_aius@43.166.187.96` 对目标 `kunlunads_dev` 只有 SELECT/SHOW VIEW，没有本方案需要的三表迁移、写入或建账号/授权能力。别的 schema 上的权限不能转用；Linux root SSH 不等于数据库管理员。

请由数据库管理员把有目标库授权能力的管理连接配置放在 CPU 服务器的 root-owned 0600 文件里，并告知绝对路径；不需要在聊天中发密码。也可由管理员直接创建方案限定的 migrator/writer 两个账号并提供服务器上的安全配置路径。代理会通过 SSH 完成剩余账号/owner/RPC 配置，用户无需自行理解或操作这些服务。精确三表、权限与文件合同见 [数据库授权说明](db-access-blocker-20260827.md)。

### 历史继续步骤（旧迁移方案不可执行）

1. 重新核对 CPU 候选 40042f9、目标库、当前队列、备份和演练证据时效；旧 c719 演练不能改绑，按新候选重验/更新，不改写旧证据。正式 API 和任务 worker 均需配置 CPU 本地 manifest 路径与固定 SHA，HK 不接收业务查询配置。
2. 完成最小权限账号、生产三表迁移、18837 鉴权 writer/RPC 与实际服务身份健康验证；备份迁移 CPU SQLite、发布精确 CPU 候选。
3. HK 当前仍为 `drama-synthesis-canary/20260827` 隔离 COS 前缀；备份配置后显式切回正式 `drama-materials` 前缀并验证，仅重启本次新增服务。CPU drain 后切向 18788，不做双写或静默 fallback。
4. 仅在 **Shahrul Ikmal**（channel `UCHJ1jFaYuW8g5EM7hM5pPpg`）执行已授权的单次内部 unlisted 测试：描述 `{{url}}` 短链、一条评论、三表回读和幂等确认。正式 public 测试不在授权内；未完成前 live/sync 保持关闭。
5. 指定测试全部通过后再完成正式功能放行与业务验收。剩余 CPU/YouTube 集成目前尚未验证，不保证不会发现新的问题。

### 历史安全停止与保留说明（不触发本次服务操作）

当前 CPU 尚未切流，停止新增 HK worker/tunnel 即可保留原业务；保留新代码、模型、素材、备份、manifest 和 COS 证据。不自动删除外部资源或反向 DDL。

旧 c719beb 缓存不理解新版本 manifest 的精确长度合同，不能盲退二进制后让旧 worker 重放已完成的新 job。若需要退二进制，先冻结任务并审查；CPU SSH key 回退须核对前后 SHA，遇并发变更即停，不覆盖别人的 key。维护技能的现行上下文已补记环境隔离与发布门禁，未修改用户记忆库。
