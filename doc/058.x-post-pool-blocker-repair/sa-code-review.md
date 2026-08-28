# 代码评审

## 结论

独立只读评审未发现可证实的P1/P2。复核新增两个helper、媒体准备/二次verify/credentials顺序、实际队列fence、FIFO/短剧归属与relay锁归属。

## 已关闭问题

- 只读available_drama_pool_items错误曾携带unknown标志：已改为精确409，保留全池needs_review保护，真实本地HTTP用例通过。
- schedule内部请求timeout原上限900不足以覆盖GPU准备：独立上限7200，默认及Daily不变，配置测试通过。
- 新错误码未登记：已补中文说明并将新增媒体模块纳入catalog扫描。
- 历史恢复source切换缺少明确拒绝：增加素材ID+URL与原队列完全一致校验和故障用例。

## 生产兼容性

Sidecar为18d9cfb；主API共享service.py较旧，差异仅为尚未同步的恢复审计、租约、容量证明等代码，未发现独立新功能。发布前保留其备份，仅同步共享service.py和新增两个helper，不改主API selector/OAuth/app或其他功能。备份副本先执行初始化并确认业务数量不变，再切换代码；历史媒体恢复独立进行，live apply之前必须先在副本演练通过。
