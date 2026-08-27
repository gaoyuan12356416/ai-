# SA 代码评审

## 2026-08-27 CPU-only catalog 增量

独立审查新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 的 4 个冻结文件，未发现新增 P0/P1。七套一次合并 204/204 PASS（13.639 秒），另 15 项文件/JSON/no-fallback 对抗 PASS；3 Python 文件 compile/3.9 AST、diff-check、测试前后冻结 SHA 一致。root 随后经 SSH 在 CPU 独立 checkout 验证真实 manifest 与原函数，无生产应用/数据库变更；两类证据不混称。

CPU 的 HTTP catalog proxy 已删除；目录仅本地元数据，失败不回 GPU/素材包。GPU 正常调用链无业务查询，缺参拒绝，专用 worker 不启动 `app.main()`；制作本地校验及 COS/cache I/O 保留。共享 app 导入仍有正常日志/锁初始化，不能表述为绝对无副作用。

既有非阻断 P2：共享下载器可接受输入 URL 并跟随重定向，正常业务边界不等于网络级来源强隔离；恶意服务令牌持有者或错误媒体跳转可能产生非预期 HTTP。后续如加固需 drama 专用来源/路径/重定向 allowlist，不扩改 X/FB/TT 共用下载器。未发现实际 GPU 业务查询事件。数据库合法写权限仍缺，不能因代码 PASS 放开正式流量/YouTube 测试。见 [完整边界记录](cpu-gpu-boundary-20260827.md)。

## 历史 Wave8

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 独立代码/安全 QA PASS，0 P0/P1。已覆盖 immutable recipe/source、DOM text-only audit、历史迁移事务、gy namespace/atomic no-overwrite、URL order/fbclid、macro pre-mutation、processing/visibility、unknown reconciliation、comment-only retry、strict writer identity/payload、outbox fencing、secret redaction与 HK 无 YouTube credential。

以上 Wave8 结论及其后的线上实查增量 code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 均已独立复审 PASS，增量 P0/P1/P2=0/0/0；这仍不构成 production release PASS。增量已完成 gy owner/root/namespace 基础配置，并关闭 18836 冲突及统一表权限/schema/credential/MySQL57 能力面缺口；证据以 `test-report.md` 为准。固定 public 合规风险仍保留到最终业务验收；未执行真实短链、MySQL 写入或 YouTube 外部发布。
