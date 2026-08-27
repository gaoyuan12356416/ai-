# 缺陷记录

## 2026-08-27 当前状态

CPU 查询边界增量 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 已提交推送：原模板目录向 GPU 查询的缺口（BUG-022）已修复，独立七套 204/204 PASS（13.639 秒）、另 15 项对抗单列 PASS，CPU 3.9.6 真实只读 manifest/原函数验证 PASS；生产 API 未替换/重启。详情见 [职责记录](../cpu-gpu-boundary-20260827.md)。

HK 当前独立 release `e1f5a1d04cfb510df9c2444ac592adec2827508b` 已 GitHub-first 部署。v3 auto/manual 共 4 输出、两个随机 720×1280/High/5 秒/150 帧、完整下载解码/封面 callback，以及真实重启 worker+tunnel 后两 job 幂等复用均 PASS。报告及 8 PNG 经独立只读复核无新阻断；该复核不另算真机执行。c719 阶段重复 POST 失败已由 BUG-020 闭环，不再是当前 HK 阻塞。

历史 HK 六套代码 QA 共 188 项：首次 187 PASS + 1 项文档文本合同 FAIL（13.567 秒），文档补回后只复测该失败项 1/1 PASS（0.050 秒）；不是一次整套全绿。27 文件语法/Python 3.9 AST PASS；此批、旧 166/focused 22 和五项内存 mock 对抗均不与本轮 204 叠加。

整体正式发布仍 HOLD：生产 ads_aius 对 kunlunads_dev 仅 SELECT/SHOW VIEW，无合法 admin/migrator/writer；CPU 主应用未部署/重启、仍为 18787，真实 YouTube 上传/评论为 0。HK COS 仍为 drama-synthesis-canary/20260827，正式激活需独立备份配置、切 production prefix 后验收。299309 行历史三表恢复演练仍精确绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，不改绑新 CPU 40042f9 或 HK e1f5，不等同全集群灾备。

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
