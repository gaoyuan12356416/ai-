# 缺陷记录

## 2026-08-27 当前状态

代码候选 `c719bebf72be900ec3853858dc53b36b83beffd2` 的独立合并回归 166/166、25 文件语法/Python 3.9 AST PASS；另五项内存 mock 媒体对抗 PASS，不并入 166。代码缺陷关闭不代表整体验收完成：HK 真实第二次同 job/payload POST 重复制作，新的运行幂等缺陷由主代理另立记录；生产 admin/migrator/writer 权限仍硬阻塞，CPU 未切流，真正 YouTube 上传/评论为 0。

用户已授权 SSH-only 的部署及指定 Shahrul Ikmal 一次 unlisted 视频、一条评论；不是未授权 canary，也不允许 public 测试。统一现行结论见 [测试报告](../test-report.md)、[迁移证据](../migration.md) 和 [HK 实测](../hk-gpu-setup-20260827.md)。

| 缺陷 | 当前状态与验收边界 |
| --- | --- |
| [BUG-012](BUG-012.md) HK 运行包缺口 | 运行包实现与独立 QA 已补齐；原发现记录不代表当前整机状态，最终运行放行仍受新幂等缺陷及完整媒体复验约束 |
| [BUG-013](BUG-013.md) 内部 unlisted 链路缺失 | 代码修复/独立 QA PASS；指定真实 canary 已授权，待生产权限与运行门禁解除 |
| [BUG-014](BUG-014.md) 恢复演练门禁范围错误 | 代码修复/独立 QA 及真实三表恢复演练 PASS；不代表生产权限或全集群灾备通过 |
| [BUG-015](BUG-015.md) Demucs 非有限归一化 | 修复/离线边界回归 PASS；专用用户、四模型真实 CUDA 的静音 44100 帧/peak 0、反相 88200 帧/peak 0.0079345703125 均 finite，非假模型 |
| [BUG-016](BUG-016.md) 随机滤镜线程超限 | 修复/独立 QA、同沙箱对照及 HK auto 随机制作通过；完整发布仍 HOLD |
| [BUG-017](BUG-017.md) 随机模板片头时间轴 | 修复/独立 QA、五项独立 mock 对抗与 HK auto 5 秒/150 帧/解码通过；完整双模式与幂等验收未放行 |
| [BUG-018](BUG-018.md) 隐私漂移仍统一同步 | P1 代码修复并独立复核通过；fresh unlisted 不成立时零 outbox claim，非真实平台事故 |
| [BUG-019](BUG-019.md) writer 未健康即上传 | P1 代码修复并独立复核通过；只读 health/schema/身份/权限未通过时零 claim/refresh/upload，非真实平台事故 |

## 历史记录（不叠加本轮统计）

BUG-001 至 BUG-005 是上一候选历史证据，均为 superseded；旧字段、路由和状态名不能作为现行合同。旧候选 `f05e10f`、`2df9aef`、`d27c82c` 为 HOLD/obsolete。BUG-006 至 BUG-011 已在 2026-08-26 Wave8 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 经独立 QA 关闭；其测试数及当时仅 gy 基础配置已部署的状态仅保留为历史，不能覆盖后来发现的代码/真实运行缺陷。
