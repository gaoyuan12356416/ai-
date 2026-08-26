# 测试报告

状态：Wave8 候选 `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 独立 QA PASS，候选 P0/P1 为 0；production HOLD 仅剩外部门禁。本结论不授权部署、短链外写或真实 YouTube 上传/评论。

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

代码候选已通过独立 QA，但不是 production release PASS。外部门禁仍为：gy `/s2l/youtube` app-owned writer/root 与数字 namespace owner freeze；外部三张统一表及受控 RPC/schema/credential；固定 public 的 YouTube minimum functionality 合规风险接受或整改。真实 YouTube 上传/评论仍需另行精确授权。
