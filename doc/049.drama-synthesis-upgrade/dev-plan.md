# 开发计划

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 实现与独立 QA 已完成，0 P0/P1；production HOLD external-only，尚未部署或外写。

1. 冻结 requirement/SA/API/schema/test/deploy 合同并清除旧域、旧 route、旧 result、video-ID-success 描述。
2. random template 收口到 `advanced_options`，复用 FB v3，冻结 source/output identity；补历史 outputs_json 迁移。
3. 建立 material selection、Clipboard fallback 与 `drama_material_short_link`，实现 gy namespace、严格目标/fbclid 和原子 writer。
4. YouTube 迁到 exact 表名/完整状态，补 description bytes/macro、processing/visibility、comment-only retry、sync outbox 和 fail-closed unified adapter。
5. 提供隔离 HK worker、exact release/current systemd/tunnel/nginx/env 候选，保留 legacy 18787 与 `ads_video_producer.service`。
6. 替换旧 focused tests；执行 fake/temp 测试、Python 3.9 parse/compile、JS syntax/browser-safe、相关 broad、migration concurrency、diff/secret/scope 检查。

## 数据变更

- `ensure_storage` 仅 additive；PRAGMA 检查后补列。不删除旧候选表，保证回滚可读。
- 独立迁移脚本要求绝对 backup、dry-run、`BEGIN IMMEDIATE`、schema fingerprint、逐行 JSON 校验、幂等二次运行；异常整批 rollback。
- 开发阶段不向 CPU/HK/统一 MySQL 执行 DDL 或外部写入。

完成定义已满足代码候选部分：独立 focused 45、broad 77、Playwright 3、compile 11、Python 3.9 AST 11、spec syntax 1、inline JS 4、writer 3+26、outbox 9 全部 PASS。生产完成仍依赖 gy writer/root/namespace owner、统一表 RPC/schema/credential 与 fixed-public 合规门禁；当前不 deploy/push/外写。
