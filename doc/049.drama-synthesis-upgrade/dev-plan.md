# 开发计划

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 与线上实查增量 code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 已独立 QA PASS，增量 P0/P1/P2=0/0/0。仅 gy 隔离目录/Nginx 基础配置已上线；候选应用、RPC、DDL 和外部发布均未上线。

1. 冻结 requirement/SA/API/schema/test/deploy 合同并清除旧域、旧 route、旧 result、video-ID-success 描述。
2. random template 收口到 `advanced_options`，复用 FB v3，冻结 source/output identity；补历史 outputs_json 迁移。
3. 建立 material selection、Clipboard fallback 与 `drama_material_short_link`；复用现有 X 渠道 `gy.g2flow.com` TLS server，仅增加隔离的 `/s2l/youtube/` namespace，并实现严格目标/fbclid 和原子 writer。
4. YouTube 迁到 exact 表名/完整状态，补 description bytes/macro、processing/visibility、comment-only retry、sync outbox 和 fail-closed unified adapter；loopback writer 使用独立 18837，并以一次性 migrator/长期 writer、精确 schema/grant/0600 门禁隔离 DDL 与运行权限。
5. 提供隔离 HK worker、exact release/current systemd/tunnel/nginx/env 候选，保留 legacy 18787 与 `ads_video_producer.service`。
6. 替换旧 focused tests；执行 fake/temp 测试、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、migration concurrency、diff/secret/scope 检查。

## 数据变更

- `ensure_storage` 仅 additive；PRAGMA 检查后补列。不删除旧候选表，保证回滚可读。
- 独立迁移脚本要求绝对 backup、dry-run、`BEGIN IMMEDIATE`、schema fingerprint、逐行 JSON 校验、幂等二次运行；异常整批 rollback。
- 开发阶段不向 HK/统一 MySQL 执行 DDL 或外部写入；CPU 只完成已授权的 gy 隔离目录/Nginx 基础配置，未部署候选应用或生成短链文件。

Wave8 完成定义已满足：独立 focused 45、broad 77、Playwright 3、compile 11、Python 3.9 AST 11、spec syntax 1、inline JS 4、writer 3+26、outbox 9 全部 PASS。增量首次独立评审 HOLD 已发现并推动修复端口/权限/schema/credential/ACL/rollback；第四轮最终复审 PASS，P0/P1/P2=0/0/0。immutable code SHA 的实现者 unittest 91/91、Playwright 3/3、CPU Python 3.9.6 compile 7/7，以及 live 45-column fingerprint/ACL/MySQL57 capability、diff/secret 均 PASS。生产完成仍依赖统一表 migration/RPC/credential 与 fixed-public 合规门禁；当前不部署候选应用、不执行 MySQL 写入或真实 YouTube 发布。
