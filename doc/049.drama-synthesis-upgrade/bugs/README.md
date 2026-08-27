# 缺陷记录

## 最终验收状态（2026-08-27 17:50）

真实指定频道视频/评论/ads_ai新表记录与完成后幂等复验通过；最终加载反馈修正已实测，正式live/sync已开启，无剩余放行阻断，详见[上线验收](../release-acceptance-20260827.md)。下方旧权限要求是历史，不再执行。

| 缺陷 | 当前状态 |
| --- | --- |
| [BUG-027](BUG-027.md) 专用账号门禁 | SUPERSEDED BY REQUIREMENT CHANGE，用户明确取消，不需要管理员配置 |
| [BUG-028](BUG-028.md) OAuth JSON传输 | CLOSED / PRODUCTION VERIFIED，实际Token不变，同一canary成功 |
| [BUG-029](BUG-029.md) 列表入口遗漏 | CLOSED / PRODUCTION VERIFIED，三项入口已从线上列表实测 |
| [BUG-030](BUG-030.md) 频道加载无弹窗反馈 | CLOSED / PRODUCTION VERIFIED，独立增量及线上加载/完成态通过；不改变发布后端 |

## 最新覆盖决定（2026-08-27）：BUG-027 被需求变更取代

用户明确取消专用数据库账号隔离，授权现有凭据与 token 发布、只写 ads_ai 新三表。BUG-027 为 SUPERSEDED BY REQUIREMENT CHANGE，不是账号创建成功；无需管理员支持，不再执行下方账号步骤。现行 RPC v3 使用共享账号与应用 SQL 白名单并如实声明 db_least_privilege=false，原表不写。当前部署与实际发布验收另见 [现行合同](../ads-ai-new-tables-20260827.md)。

## 历史状态（16:06，北京时间；下列账号步骤已退休）

新三表已完成，原表零写，整体正式发布仍 HOLD。当前唯一已确认的部署权限阻断是**创建专用 DB writer 账号**，不是 `ads_ai` 无写权限。候选 `6e29ba8c07c75dea98caff4d1d2b4fba0ac9df1f` 已 GitHub/readback、CPU clean；16:00 fresh-table rehearsal 9 checks PASS，16:01:35 生产 63353 仅 CREATE `ads_ai.ads_youtube_videos`、`ads_ai.ads_youtube_comments`、`ads_ai.ads_youtube_publish_log` 成功且完整兼容，apply 后管理员验证无 trigger/FK；16:06 经 63350 核验三表各 0 行。

| 当前缺陷 | 状态与验收边界 |
| --- | --- |
| [BUG-027](BUG-027.md) 专用 writer 建账号权限前提遗漏 | P1 / OPEN / BLOCKED，部署前提缺漏，非代码问题。16:04:58 实际 `ads_aius@43.166.187.96` 一次精确新 writer GRANT 返回 1410，`global CREATE_USER=false`；隔离 MySQL 5.7.44 schema ALL + GRANT OPTION 账号同样复现。文档已纠正，合法 DB admin 显式 CREATE USER、精确三表授权及 runtime 健康尚待执行 |

只需合法 DB admin 提供 CPU 上 root-owned 0600 管理连接文件的绝对路径，不在聊天发送密码。必须先核实账号不存在，再 CREATE USER，然后只给 `drama_youtube_writer@43.166.187.96` 上述三表的 `SELECT, INSERT, UPDATE`；账号已有即停核实，不 ALTER/重置、不以 `ads_aius` 运行服务、不进云控制台。不再需要 `kunlunads_dev` 迁移、旧表备份或旧 migrator。

新 writer/RPC 未安装或启动，未配置广权限 runtime 凭据；CPU app 仍 `a956fb...675a3`（缩写），API PID 3841722 / job worker PID 1212、20 done、SQLite `quick_check=ok`、无 YouTube 账本。真实上传 0、评论 0，HK 未改。现行结论见 [部署状态页](../deployment-status-20260827.md)、[数据库授权说明](../db-access-blocker-20260827.md) 和 [新表合同](../ads-ai-new-tables-20260827.md)。

## 历史状态：新表建成前的代码与 HK 验证

下文保留各阶段测试和旧阻断记录，不叠加测试数，不代表当前部署状态；其中旧库权限/备份/迁移要求已退休，当前权限门禁以上文及 BUG-027 为准。

最新 ads_ai 新表增量发现并修复 [BUG-023](BUG-023.md)（镜像 digest 接口）、[BUG-024](BUG-024.md)（P1 datadir 隔离）和 [BUG-025](BUG-025.md)（真实飞书操作者字符串）；独立完整 10 模块 262/262 PASS，实机建表/部署证据另列，不复用下文旧库权限门禁。见 [新表合同](../ads-ai-new-tables-20260827.md)。

CPU 查询边界增量 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 已提交推送：原模板目录向 GPU 查询的缺口（BUG-022）已修复，独立七套 204/204 PASS（13.639 秒）、另 15 项对抗单列 PASS，CPU 3.9.6 真实只读 manifest/原函数验证 PASS；生产 API 未替换/重启。详情见 [职责记录](../cpu-gpu-boundary-20260827.md)。

HK 当前独立 release `e1f5a1d04cfb510df9c2444ac592adec2827508b` 已 GitHub-first 部署。v3 auto/manual 共 4 输出、两个随机 720×1280/High/5 秒/150 帧、完整下载解码/封面 callback，以及真实重启 worker+tunnel 后两 job 幂等复用均 PASS。报告及 8 PNG 经独立只读复核无新阻断；该复核不另算真机执行。c719 阶段重复 POST 失败已由 BUG-020 闭环，不再是当前 HK 阻塞。

历史 HK 六套代码 QA 共 188 项：首次 187 PASS + 1 项文档文本合同 FAIL（13.567 秒），文档补回后只复测该失败项 1/1 PASS（0.050 秒）；不是一次整套全绿。27 文件语法/Python 3.9 AST PASS；此批、旧 166/focused 22 和五项内存 mock 对抗均不与本轮 204 叠加。

旧范围当时 HOLD 的记录（已被新表范围取代）：生产 ads_aius 对 kunlunads_dev 仅 SELECT/SHOW VIEW，无合法 admin/migrator/writer；CPU 主应用未部署/重启、仍为 18787，真实 YouTube 上传/评论为 0。HK COS 仍为 drama-synthesis-canary/20260827，正式激活需独立备份配置、切 production prefix 后验收。299309 行历史三表恢复演练仍精确绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，不改绑新 CPU 40042f9 或 HK e1f5，不等同全集群灾备，也不作为当前继续门禁。

用户已授权 SSH-only 的部署及指定 Shahrul Ikmal 一次 unlisted 视频、一条评论；待合法权限/凭据与健康/身份门禁，不是缺少这次测试授权，也不允许 public 测试。不进入云控制台、不动旧 X/ads 业务。统一现行结论见 [部署状态页](../deployment-status-20260827.md)、[测试报告](../test-report.md)、[迁移证据](../migration.md) 和 [HK 实测](../hk-gpu-setup-20260827.md)。

| 缺陷 | 当前状态与验收边界 |
| --- | --- |
| [BUG-012](BUG-012.md) HK 运行包缺口 | 运行包、独立 QA 及 e1f5 双模式/重启复用实测闭环；仅 HK 隔离运行验收，不代表正式激活 |
| [BUG-013](BUG-013.md) 内部 unlisted 链路缺失 | 代码修复/独立 QA PASS；指定真实 canary 已授权，待生产权限与运行门禁解除 |
| [BUG-014](BUG-014.md) 恢复演练门禁范围错误 | 代码修复/独立 QA 及真实三表恢复演练 PASS；不代表生产权限或全集群灾备通过 |
| [BUG-015](BUG-015.md) Demucs 非有限归一化 | 修复/离线边界回归 PASS；专用用户、四模型真实 CUDA 的静音 44100 帧/peak 0、反相 88200 帧/peak 0.0079345703125 均 finite，非假模型 |
| [BUG-016](BUG-016.md) 随机滤镜线程超限 | 修复/独立 QA、同沙箱对照及 e1f5 auto/manual 随机制作通过；不扩大线程预算、不改旧 FB/ads 服务 |
| [BUG-017](BUG-017.md) 随机模板片头时间轴 | 修复/独立 QA、五项历史 mock 对抗及 e1f5 双模式 5 秒/150 帧/下载解码、报告+8PNG 复核通过 |
| [BUG-018](BUG-018.md) 隐私漂移仍统一同步 | P1 代码修复并独立复核通过；fresh unlisted 不成立时零 outbox claim，非真实平台事故 |
| [BUG-019](BUG-019.md) writer 未健康即上传 | P1 代码修复并独立复核通过；只读 health/schema/身份/权限未通过时零 claim/refresh/upload，非真实平台事故 |
| [BUG-020](BUG-020.md) GPU 小产物缓存重制 | P1 实现/独立/真机闭环；e1f5 metadata 精确 URL/size 与 HEAD 验证，重启后 fixture HTTP 服务未运行时两 job 仍复用且不改 manifest/workdir |
| [BUG-021](BUG-021.md) 随机缓存 profile 校验缺失 | P2 已修复；缺失/错值在 HEAD 前阻断由离线独立对抗证明，有效 profile 由真机缓存命中验证；不重制/上传坏缓存 |
| [BUG-022](BUG-022.md) CPU 模板目录仍请求 GPU | 边界缺口代码修复/独立 QA、CPU 真实目录验证 PASS；缺配置 503 无 fallback。完整生产切换待既有权限门禁，不以代码 PASS 代替上线 |

## 历史记录（不叠加本轮统计）

BUG-001 至 BUG-005 是上一候选历史证据，均为 superseded；旧字段、路由和状态名不能作为现行合同。旧候选 `f05e10f`、`2df9aef`、`d27c82c` 为 HOLD/obsolete。BUG-006 至 BUG-011 已在 2026-08-26 Wave8 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 经独立 QA 关闭；其测试数及当时仅 gy 基础配置已部署的状态仅保留为历史，不能覆盖后来发现的代码/真实运行缺陷。

2026-08-27 c719 的 166/166、25 文件语法与五项 mock 对抗属于上一轮；它们没有提前证明后来新增的缓存修复。c719 的真实重复 POST FAIL 已由 e1f5 的 v3 + restart replay 证据取代；历史失败保留用于解释 BUG-020，不继续列作当前未修复项。
