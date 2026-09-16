# 用例评审

补充独立评审发现的缺口：非法emoji组合计数、真实XAccountsClientError.status_code、发送完成后台账暂时不可写、首次明确409的UI恢复。分别要求真实异常类、同进程恢复、零重复网络调用及未知结果不解锁断言。

使用实际app handler AST校验认证/同源，实际YouTubeWorkflow校验归属，临时真实Sidecar SQLite验证桥接与幂等，避免只测试模拟实现。
