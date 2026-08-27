# 开发与交付计划（2026-08-27 现行）

已独立 QA、GitHub push/readback 的候选为 `c719bebf72be900ec3853858dc53b36b83beffd2`。用户当前授权为环境完成后继续部署，以及 Shahrul Ikmal 的一次内部 unlisted 视频与一条评论；正式 public 测试不在授权内。全部支持操作仅 SSH，禁止腾讯云管理后台。代码完成与真实运行完成分别验收，不使用旧“仅剩外部门禁”口径。

| 交付阶段 | 当前结果与剩余条件 |
| --- | --- |
| 本地实现与独立 QA | c719beb 合并回归 166/166、25 文件语法/Python 3.9 AST PASS；另五项内存 mock 媒体对抗 PASS，不叠加计数 |
| HK 独立 dark release | c719beb 已运行；真实 auto 三种媒体及 COS 三文件下载、5 秒/150 帧/解码通过；重复同 job/payload POST 幂等失败，缓存窄修仅本地完成，独立回归/发布/完整复验待完成 |
| 三表数据保护 | SSH 真实 snapshot + CPU loopback MySQL 5.7.44 恢复/迁移演练 PASS，299309 行；精确 SHA 与时效见 migration.md，非全集群灾备 |
| 生产 DDL/writer/RPC | HOLD：ads_aius 只有 SELECT/SHOW VIEW，无合法 admin/migrator/writer；演练成功不赋予权限 |
| CPU 正式部署/切流 | 尚未切流，保留 legacy 18787；必须先通过媒体幂等、迁移/权限、writer 健康及队列 drain 门禁 |
| 指定真实 canary | 已授权但尚未执行；0 真实上传/评论，待前置门禁通过后仅推进固定 operation/task |

HK 后续窄修可形成新的独立 release；已通过的 CPU 三表演练仍精确绑定 c719beb，不能将旧证据改写成新 SHA。实时结果由主代理补充 [测试报告](test-report.md) 与 [HK 实测记录](hk-gpu-setup-20260827.md)。

## 已实现工作范围

1. 冻结 requirement/SA/API/schema/test/deploy 合同并清除旧域、旧 route、旧 result、video-ID-success 描述。
2. random template 收口到 `advanced_options`，复用 FB v3，冻结 source/output identity；补历史 outputs_json 迁移。
3. 建立 material selection、Clipboard fallback 与 `drama_material_short_link`；复用现有 X 渠道 `gy.g2flow.com` TLS server，仅增加隔离的 `/s2l/youtube/` namespace，并实现严格目标/fbclid 和原子 writer。
4. YouTube 迁到 exact 表名/完整状态，补 description bytes/macro、processing/visibility、comment-only retry、sync outbox 和 fail-closed unified adapter；loopback writer 使用独立 18837，并以一次性 migrator/长期 writer、精确 schema/grant/0600 门禁隔离 DDL 与运行权限。
5. 提供隔离 HK worker、exact release/current systemd/tunnel/nginx/env 候选，保留 legacy 18787 与 `ads_video_producer.service`。
6. 替换旧 focused tests；执行 fake/temp 测试、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、migration concurrency、diff/secret/scope 检查。
7. 补齐无 /root 依赖的 HK runtime/service/env、本地 Demucs 模型、并发与路径保护；修复静音归一化、随机滤镜线程和片头时间轴问题。实际重复请求发现的幂等缺陷独立处理，不被离线 QA 覆盖结论掩盖。
8. 以受控 CLI 补齐单次 unlisted canary，固定 app 1479/channel 263/account 255 和真实 job source；public HTTP/worker 隔离。修复评论真实返回合同、上传前 writer 健康门禁和同步前 fresh privacy 门禁，unknown 不盲重试。
9. 实现三表一致性 snapshot、独立 MySQL 5.7 恢复、两次 apply 幂等、历史数据/结构守恒与 SHA/时效证据；保留生产最小权限硬门禁。

## 数据变更

- `ensure_storage` 仅 additive；PRAGMA 检查后补列。不删除旧候选表，保证回滚可读。
- 独立迁移脚本要求绝对 backup、dry-run、`BEGIN IMMEDIATE`、schema fingerprint、逐行 JSON 校验、幂等二次运行；异常整批 rollback。
- 三表演练已实际写入 CPU 专用 loopback 容器，不是生产 DDL；生产表、生产 writer 和外部 YouTube 仍未因此放行。快照私有行/凭据不进入 Git，文档仅记录目录、计数与摘要。
- 本轮文档更新不执行代码、DDL、服务切换或外部发布。生产执行必须由已授权流程核对实际身份、证据有效期、精确 SHA 与账号权限。

## 历史说明

2026-08-26 Wave8 SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 与增量 `2b26b540660fd3687fa7c66e68a246d1a706136a` 的 focused/broad/浏览器/compile 等结果保留在测试报告历史段。它们不与本轮 166 例相加，旧“只授权 HK 搭建”“只部署 gy 基础配置”“canary 需另行授权”均不能作为当前部署状态或授权依据。
