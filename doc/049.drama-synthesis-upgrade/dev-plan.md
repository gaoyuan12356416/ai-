# 开发与交付计划（2026-08-27 现行）

## 最新范围与团队

在 ads_ai 新建专用三表，原库只读，替代下文旧迁移/权限门禁。root 负责集成、SSH 部署与实机验收；一名实现工程师负责写入器/bootstrap/新表专项；独立 SA/QA 负责冻结后的最终完整回归。顺序：实现与专项→独立冻结回归→GitHub 候选→全新 MySQL5.7 演练→生产 CREATE-only/最小 writer→CPU/HK 切流→唯一授权 unlisted canary。现行 [新表合同](ads-ai-new-tables-20260827.md)；下文旧库状态仅作历史。

HK 独立 release `e1f5a1d04cfb510df9c2444ac592adec2827508b` 已经 GitHub-first 部署，并完成 v3 双模式真实制作和重启后幂等复用验收。CPU 待部署候选及三表恢复演练仍精确绑定 `c719bebf72be900ec3853858dc53b36b83beffd2`，不改绑 HK 增量 SHA。整体正式发布仍 HOLD，不能把 HK dark/canary 验收等同于 CPU 正式部署完成。

用户当前授权为环境完成后继续部署，以及 Shahrul Ikmal 的一次内部 unlisted 视频与一条评论；正式 public 测试不在授权内。全部支持操作仅 SSH，禁止腾讯云管理后台，不触碰既有 X/ads_video_producer。现行总状态见 [部署状态页](deployment-status-20260827.md)。

| 交付阶段 | 当前结果与剩余条件 |
| --- | --- |
| HK 增量独立代码 QA | 六套共 188 项首次 187 PASS + 1 项文档文本合同 FAIL（13.567 秒）；补回文档后仅该失败项复测 1/1 PASS（0.050 秒）；不是一次整套全绿。27 文件语法/Python 3.9 AST PASS，不叠加旧 166 或 focused 22 |
| HK 独立 dark release | e1f5 已运行；v3 auto + manual 共 4 输出，两个随机输出均 720×1280/High/5 秒/150 帧；完整下载解码、封面 callback PASS；真实重启 worker+tunnel 后两 job 重复 POST 均 200、manifest SHA/mtime 不变、workdir 无重建 |
| HK 正式激活 | 当前 COS 仍为 drama-synthesis-canary/20260827；切 production prefix 前须独立备份配置，切换后重新验收，不能称已正式激活 |
| 三表数据保护 | SSH 真实 snapshot + CPU loopback MySQL 5.7.44 恢复/迁移演练 PASS，299309 行，绑定 c719beb；精确 SHA 与时效见 migration.md，非全集群灾备 |
| 生产 DDL/writer/RPC | HOLD：ads_aius 只有 SELECT/SHOW VIEW，无合法 admin/migrator/writer；演练成功不赋予权限 |
| CPU 正式部署/切流 | 未部署/重启主应用，继续 legacy 18787；待合法迁移/写入权限、writer 健康、正式前缀及队列 drain 等前置门禁 |
| 指定真实 canary | 已有单次测试授权但未执行；0 真实上传/评论，待合法权限/凭据与健康/身份门禁后仅推进固定 operation/task |

HK fresh 报告记录 79.44 秒，随后真实重启 worker+tunnel；fixture HTTP 服务未运行时 replay 单元 1.710 秒、Result=success。报告 `/data/drama-synthesis-gpu/work/acceptance/http-media-20260827-v3.json` 的 SHA-256 为 `40746316694eeb4d34fb4511713acb13b8de14cff382f6116fdd99f1351f2175`。独立 QA 已只读核对报告和 8 张 PNG，无新阻断；该核对不算再次运行真机。完整证据见 [测试报告](test-report.md) 与 [HK 实测记录](hk-gpu-setup-20260827.md)。

## 已实现工作范围

1. 冻结 requirement/SA/API/schema/test/deploy 合同并清除旧域、旧 route、旧 result、video-ID-success 描述。
2. random template 收口到 `advanced_options`，复用 FB v3，冻结 source/output identity；补历史 outputs_json 迁移。
3. 建立 material selection、Clipboard fallback 与 `drama_material_short_link`；复用现有 X 渠道 `gy.g2flow.com` TLS server，仅增加隔离的 `/s2l/youtube/` namespace，并实现严格目标/fbclid 和原子 writer。
4. YouTube 迁到 exact 表名/完整状态，补 description bytes/macro、processing/visibility、comment-only retry、sync outbox 和 fail-closed unified adapter；loopback writer 使用独立 18837，并以一次性 migrator/长期 writer、精确 schema/grant/0600 门禁隔离 DDL 与运行权限。
5. 提供隔离 HK worker、exact release/current systemd/tunnel/nginx/env 候选，保留 legacy 18787 与 `ads_video_producer.service`。
6. 替换旧 focused tests；执行 fake/temp 测试、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、migration concurrency、diff/secret/scope 检查。
7. 补齐无 /root 依赖的 HK runtime/service/env、本地 Demucs 模型、并发与路径保护；修复静音归一化、随机滤镜线程和片头时间轴问题。c719 阶段真实重复 POST 发现的幂等问题已由 e1f5 修复并完成重启复用验收，见 BUG-020。
8. 以受控 CLI 补齐单次 unlisted canary，固定 app 1479/channel 263/account 255 和真实 job source；public HTTP/worker 隔离。修复评论真实返回合同、上传前 writer 健康门禁和同步前 fresh privacy 门禁，unknown 不盲重试。
9. 实现三表一致性 snapshot、独立 MySQL 5.7 恢复、两次 apply 幂等、历史数据/结构守恒与 SHA/时效证据；保留生产最小权限硬门禁。
10. 缓存 metadata 绑定产物 URL 与实际 size，HEAD 必须 200、无 redirect、精确 length；坏缓存或暂时不通禁止重制覆盖，legacy 1 MiB 门槛保留。随机 profile 必填且精确校验，缺失/错值在 HEAD 前阻断；BUG-020/021 已完成实现、独立验证和真机回归闭环。

## 数据变更

- `ensure_storage` 仅 additive；PRAGMA 检查后补列。不删除旧候选表，保证回滚可读。
- 独立迁移脚本要求绝对 backup、dry-run、`BEGIN IMMEDIATE`、schema fingerprint、逐行 JSON 校验、幂等二次运行；异常整批 rollback。
- 三表演练已实际写入 CPU 专用 loopback 容器，不是生产 DDL；生产表、生产 writer 和外部 YouTube 仍未因此放行。快照私有行/凭据不进入 Git，文档仅记录目录、计数与摘要。
- 本轮文档更新不执行代码、DDL、服务切换或外部发布。生产执行必须由已授权流程核对实际身份、证据有效期、精确 SHA 与账号权限。

## 历史说明

2026-08-26 Wave8 SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 与增量 `2b26b540660fd3687fa7c66e68a246d1a706136a` 的 focused/broad/浏览器/compile 等结果，以及 2026-08-27 c719 的 166/166、25 文件语法、五项内存媒体对抗和后续 focused 22，均按独立轮次保留，不与最新 188 项相加。c719 的重复 POST 失败已由 e1f5 的真机验收取代；旧“只授权 HK 搭建”“canary 需另行授权”不再是当前授权依据。
