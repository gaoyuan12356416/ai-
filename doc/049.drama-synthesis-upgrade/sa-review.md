# SA 需求评审

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 需求/架构合同经独立 QA PASS，0 P0/P1；production HOLD 仅剩下述外部门禁。

## 架构决策

- CPU 是任务、OAuth、发布、审计和同步账本唯一协调端；HK 是无 YouTube 凭据的媒体执行端。
- 复用 FB v3 immutable manifest/recipe，业务 recipe 额外冻结 source。
- gy 使用独立 `/s2l/youtube/` namespace 和 app-owned root，避免与 X/TT/FB writer 冲突。
- SQLite 保证短链/发布/outbox 幂等；统一表使用严格白名单适配器，缺依赖时 sync fail closed。
- video ID 不构成成功；processing 与 visibility 读回是 published 门槛。

## 风险门禁

- 生产已在现有 `gy.g2flow.com` TLS server 内加入隔离 `/s2l/youtube` location，并建立 app-owned root；未创建域名/DNS/证书，未生成真实短链文件。正式代码切换前仍须校验 writer owner、Nginx 只读与 X 路由不变。
- 三张统一 YouTube legacy 表已确认存在于 `kunlunads_dev`；当前运行只读账号没有写权限，三表也尚无 external-id 幂等列。须使用一次性 migrator 完成 additive column/index migration并销毁该账号，再创建从未持有 DDL 的长期 writer；`127.0.0.1:18837` health 必须验证全量 schema/index 与三表级 SELECT/INSERT/UPDATE 精确授权后才可打开 sync。18836 保留现有 FB 隧道。
- 固定 public 存在 YouTube 最低功能要求合规风险；正式启用前须接受或整改。
- 真实 YouTube 上传/评论需另行精确授权，代码部署授权不包含外部发布。
- 本轮仅部署了现有 `gy.g2flow.com` 下的隔离目录/Nginx 基础配置；没有部署本候选应用代码，没有生成短链文件、统一表写入或真实 YouTube 上传/评论；fixed-public 风险未因代码 QA PASS 而自动接受。
