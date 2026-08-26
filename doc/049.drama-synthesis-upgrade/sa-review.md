# SA 需求评审

状态：实现者合同已完成；独立 QA/SA 复审待执行，不自签通过。

## 架构决策

- CPU 是任务、OAuth、发布、审计和同步账本唯一协调端；HK 是无 YouTube 凭据的媒体执行端。
- 复用 FB v3 immutable manifest/recipe，业务 recipe 额外冻结 source。
- gy 使用独立 `/s2l/youtube/` namespace 和 app-owned root，避免与 X/TT/FB writer 冲突。
- SQLite 保证短链/发布/outbox 幂等；统一表使用严格白名单适配器，缺依赖时 sync fail closed。
- video ID 不构成成功；processing 与 visibility 读回是 published 门槛。

## 风险门禁

- 生产尚无 `/s2l/youtube` location/root/owner；候选仅提供配置，部署时必须单独创建并验证。
- 三张统一 YouTube 表当前不存在；不得缩减合同。缺表时 outbox 保留并报告失败，由外部 owner 提供 schema 后才可完成同步门禁。
- 固定 public 存在 YouTube 最低功能要求合规风险；正式启用前须接受或整改。
- 真实 YouTube 上传/评论需另行精确授权，代码部署授权不包含外部发布。
