# 代码审查

已实施审查项：GraphError.detail 在预览/执行阻止分支保留；recheck 入参仅接受 preview_id/request_id；执行和重核验先持久化互斥；对象结果保留原尝试审计；Video阶段新代次与逐对象校验；部分索引从不发布。

BUG-001：Windows只读文件句柄fsync失败；改为r+b，真实文件回归通过。

上线前仍需记录服务器完整读取性能和生产只读验收。后续发现的问题追加本记录。
