# 013.x-post-media-repair SA 代码评审

## 结论

上线前阻断项已关闭。实现保持 fail closed，不覆盖源文件，不改变全局素材排重身份，也不会在回填阶段创建 X 发布队列。

## 评审问题

| 编号 | 级别 | 问题 | 修复 |
| --- | --- | --- | --- |
| CR-001 | P1 | HEVC 首错可能掩盖隔行扫描或大于 60fps，并被 GPU 一并修掉 | worker 对源 scan、FPS、duration 做完整检查；复合非允许问题拒绝修复 |
| CR-002 | P1 | 同日重跑在查询既有计划前重新选材，计划已提交但响应丢失时无法恢复 | 新增只读冻结计划快照；恢复分支先于账号、素材池、MySQL 和 GPU |
| CR-003 | P2 | 回填报告落盘失败会把已完成副作用误报为零尝试 | 报告路径执行前校验；运行后 I/O 失败保留真实结果并单列错误 |
| CR-004 | P2 | 六次 900 秒正好占满 90 分钟 systemd 时限 | oneshot 总时限提高到 120 分钟 |
| CR-005 | P2 | backfill 不把 COS URL 写回素材池 | 明确采用 immutable COS + GPU manifest warm-cache；daily 再取回并二次复检后才冻结进 queue |

## 安全边界

- worker 仅监听 GPU 回环地址；CPU 仅通过回环 SSH 反向隧道访问。
- daily 与 worker 使用独立 Bearer；响应、日志和回填报告均不输出 Token 或源 URL。
- 下载继续使用 HTTPS 精确 host allowlist、禁重定向和大小限制。
- COS key 由 profile、素材 ID、源 SHA 和输出 SHA 组成，不覆盖源对象。
- 同一 job 使用文件锁、GPU 单槽和 0600 manifest；只有 COS HEAD 校验成功才返回 ready。
- 发布阶段仍按原素材 ID 全局排重，并再次下载、核指纹、probe 后才调用 X。
