# 测试报告

状态：Wave8 候选 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 与线上实查增量候选 `2b26b540660fd3687fa7c66e68a246d1a706136a` 均已独立 QA PASS，增量 P0/P1/P2 为 0/0/0；production HOLD 仅剩外部门禁。本结论不授权部署、短链外写或真实 YouTube 上传/评论。

## 独立 QA 证据

2026-08-26 对 exact SHA 执行：

- focused 45/45 PASS；broad 77/77 PASS；实际 Chrome Playwright 3/3 PASS。
- compile 11/11 PASS；Python 3.9 AST 11/11 PASS；browser spec syntax 1/1 PASS；inline JS 4/4 PASS。
- unified writer：3 个正常实体合同 + 26 个 adversarial 合同用例全部 PASS；outbox malformed/fencing 9/9 PASS。
- hostile recipe 的 img/onerror/script/quotes 以文本可见，0 执行、0 DOM 注入；两 UI mirror 一致。
- 未发现 candidate P0/P1。旧候选 `f05e10f`、`2df9aef`、`d27c82c` 均为 HOLD/obsolete，不可替代 Wave8 SHA 作为发布候选。

## 实现者补充证据

- focused 45/45 PASS；相关 broad 116 collected：115 PASS、1 个 Windows POSIX permission 预期 skip、0 failure；实际 Chrome Playwright 3/3 PASS。
- 本地 py_compile 10 个文件、HK Python 3.9.6 stdin-only runtime compile 9 个文件、browser spec `node --check`、两 HTML 6 个 script block parse、static mirror、staged diff/secret/scope/artifact 检查均 PASS。
- 全部外部动作使用 temp/fake；未执行真实短链 writer、YouTube 上传/评论、统一 MySQL 写入、CPU/HK 部署。

## 发布结论

Wave8 与线上实查后的代码增量均已通过独立 QA；当前仍不是 production release PASS。gy `/s2l/youtube` app-owned root、Nginx 隔离路由和 X 兼容性门禁已完成；剩余门禁为：统一三表 additive migration、独立最小权限账号与受控 RPC 部署；固定 public 的 YouTube minimum functionality 合规风险接受或整改。真实 YouTube 上传/评论仍需另行精确授权。

## 2026-08-26 线上实查后的增量证据

- X 渠道现行机制已核对：先在 SQLite `x_post_publish_log` 预留自增 ID，以该 ID 生成 `https://gy.g2flow.com/s2l/<id>.html`，冻结 long/short URL 和正文，再原子创建不可覆盖 wrapper，成功后才进入 X 发布；抽样 ID `633` 的数据库 long URL、数字文件名与 HTML canonical 一致，现有短链返回 200。
- YouTube 不创建新域名、DNS、证书或 server block；只复用现有 `gy.g2flow.com`，增加优先级更高的 `/s2l/youtube/<数字短码>.html` 隔离路径。CPU 已建立 `drama-youtube` owner/root 与 Nginx snippet；`nginx -t` PASS，X `/s2l/633.html` 仍为 200，不存在的 YouTube 数字路径为 404，POST 为 403。未生成真实 YouTube 短链文件。
- 统一三表确认已存在于 `kunlunads_dev`：`ads_youtube_videos`、`ads_youtube_comments`、`ads_youtube_publish_log`。当前应用账号只读；增量实现提供固定白名单 loopback RPC、独立 0600 DB/RPC 凭据、三表完整 legacy 字段映射、负数 synthetic queue join，以及 external-id nullable 列/唯一索引的可审计迁移脚本。
- 首次增量独立评审结论为 HOLD：发现 writer 18836 与现有 FB 隧道硬冲突，以及 migrator/runtime 权限、精确 schema/grant、credential owner/0600、ACL 与共享库回滚合同缺口。该结论阻止了提交和部署。
- 修复候选改用经 CPU `ss`、线上配置和仓库三方核验为空闲的 18837；新增可复现 writer env、一次性 migrator、长期最小权限 writer、全量 schema/grant fingerprint、fresh backup evidence/rehearsal、exact owner/0600、短链 ACL 检查和安全回滚。实现者 Python unittest 91/91 PASS；实际 Chrome Playwright 3/3 PASS；CPU Python 3.9.6 对七个运行文件 compile PASS；线上只读 45 个 legacy 列 fingerprint 与 ACL `--check` PASS；`git diff --check`、changed-file secret scan 0 PASS。
- 线上 MySQL 已只读确认是 `5.7.18-cynos-2.1.14-log`、`@@read_only=1`、账号 host 为 `43.166.187.96`、`information_schema.ROUTINE_PRIVILEGES` 不存在、`SHOW GRANTS` 使用单引号账号。候选因此不查询不存在的表；USER/SCHEMA/TABLE/COLUMN 由 information_schema 精确闭包，routine/proxy/未知授权由 `SHOW GRANTS` 白名单拒绝。
- 第四轮最终提交前独立复审 PASS，P0/P1/P2=0/0/0；focused 46/46、RPC/migration 7/7、MySQL57 grant matrix 8/8、related broad 115 PASS + 1 预期 skip、CPU Python 3.9.6 compile、diff/secret/artifact 全部 PASS。随后对 immutable code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 再跑实现者 unittest 91/91、Playwright 3/3、CPU compile 7/7，全部 PASS。
- 上述代码 QA PASS 允许进入生产门禁执行，不等于 production release PASS，也不授权真实短链、MySQL DDL/写入或 YouTube 发布。
