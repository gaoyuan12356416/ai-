# 剧集合成升级部署状态（2026-08-27）

证据截止北京时间 12:38。香港 GPU 环境与隔离合成验收已通过；CPU 正式切换、YouTube 真实测试尚未执行，整体不能标记部署完成。支持操作仅通过 SSH，不进入腾讯云管理后台。

## 已完成

- 香港 GPU 独立 Python/CUDA/Demucs 四模型、FB-v3 四层 315 组合素材、专用服务用户、worker 与 CPU 18788 隧道搭建完成；不替换系统环境，不修改现有 X/TT/FB/ads_video_producer 服务。
- 自动/手动两种模式共 4 个真实合成产物通过下载、规格和完整解码；两个随机产物均 720×1280、5 秒、150 帧。封面回调通过。即时重复提交和服务重启后重放均复用成片，manifest 指纹/时间未变、工作目录未重建。独立 QA 已复核报告及 8 帧。
- CPU 三张统一 YouTube 表的只读一致性备份、本机隔离 MySQL 5.7.44 恢复、迁移及二次幂等演练通过：244151/53/55105 行，共 299309 行；旧结构/数据指纹保持。演练容器已停止、备份保留。这是三表恢复证据，不是全集群灾备。
- 最终只读检查显示 CPU API/job worker PID、app.py、旧 GPU 地址 18787 未变；SQLite quick_check=ok、20 done、无活动任务。现有 X 短链 200，新 YouTube 未生成路径 404/POST 403。没有真实 YouTube refresh/upload/comment，没有生成真实 YouTube 短链。

## 精确版本与证据

| 组件 | 固定版本/证据 |
| --- | --- |
| HK 当前已运行 | `e1f5a1d04cfb510df9c2444ac592adec2827508b`，GitHub-first、detached clean tree |
| CPU 待部署候选 | `c719bebf72be900ec3853858dc53b36b83beffd2`；生产仍为旧应用，不能将候选当成已上线 |
| CPU 三表演练 | 仅绑定 c719beb；`/mnt/data-disk/drama-youtube-rehearsal-20260827a11c0001/snapshot` |
| HK v3 实测报告 | `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json`；SHA `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175` |
| 代码 QA | 六套 188 项：首次 187 PASS，1 项文档文本合同修复后定向复测 PASS；不是重复整套全绿，不与基础 166 例相加 |
| HK 切换备份 | `/data/drama-synthesis-gpu/backups/20260827T123135+0800-pre-e1f5a1d` |

最终文档提交不改变上述组件版本，也不把 CPU 三表证据改绑到文档提交或 HK 版本。详细测试、产物 SHA、服务读回与回滚分别见 [测试报告](test-report.md)、[HK 记录](hk-gpu-setup-20260827.md)、[迁移](migration.md) 和 [部署](deploy.md)。

## 当前阻塞及所需支持

CPU 已有账号 `ads_aius@43.166.187.96` 对目标 `kunlunads_dev` 只有 SELECT/SHOW VIEW，没有本方案需要的三表迁移、写入或建账号/授权能力。别的 schema 上的权限不能转用；Linux root SSH 不等于数据库管理员。

请由数据库管理员把有目标库授权能力的管理连接配置放在 CPU 服务器的 root-owned 0600 文件里，并告知绝对路径；不需要在聊天中发密码。也可由管理员直接创建方案限定的 migrator/writer 两个账号并提供服务器上的安全配置路径。代理会通过 SSH 完成剩余账号/owner/RPC 配置，用户无需自行理解或操作这些服务。精确三表、权限与文件合同见 [数据库授权说明](db-access-blocker-20260827.md)。

## 得到合法授权后继续

1. 重新核对 CPU 候选、目标库、当前队列、备份和演练证据时效；过期则重新执行对应门禁，不改写旧证据。
2. 完成最小权限账号、生产三表迁移、18837 鉴权 writer/RPC 与实际服务身份健康验证；备份迁移 CPU SQLite、发布精确 CPU 候选。
3. HK 当前仍为 `drama-synthesis-canary/20260827` 隔离 COS 前缀；备份配置后显式切回正式 `drama-materials` 前缀并验证，仅重启本次新增服务。CPU drain 后切向 18788，不做双写或静默 fallback。
4. 仅在 **Shahrul Ikmal**（channel `UCHJ1jFaYuW8g5EM7hM5pPpg`）执行已授权的单次内部 unlisted 测试：描述 `{{url}}` 短链、一条评论、三表回读和幂等确认。正式 public 测试不在授权内；未完成前 live/sync 保持关闭。
5. 指定测试全部通过后再完成正式功能放行与业务验收。剩余 CPU/YouTube 集成目前尚未验证，不保证不会发现新的问题。

## 安全停止与保留

当前 CPU 尚未切流，停止新增 HK worker/tunnel 即可保留原业务；保留新代码、模型、素材、备份、manifest 和 COS 证据。不自动删除外部资源或反向 DDL。

旧 c719beb 缓存不理解新版本 manifest 的精确长度合同，不能盲退二进制后让旧 worker 重放已完成的新 job。若需要退二进制，先冻结任务并审查；CPU SSH key 回退须核对前后 SHA，遇并发变更即停，不覆盖别人的 key。维护技能的现行上下文已补记环境隔离与发布门禁，未修改用户记忆库。
