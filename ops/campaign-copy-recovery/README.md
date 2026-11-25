# Campaign Copy 首包超时核对

这些工具由值班人员在已有 SSH 管理会话中调用。适用范围为香港报表在 MySQL 返回首个握手包前超时，随后协调器留下没有数据库连接编号的租约。CPU 端的脚本使用共享 SQL 闸门和已配置的腾讯云只读接口。生产定时器负责后续报表刷新。

1. 在 CPU 上运行 `python3 collect_preauth.py`。空的 `pending` 表示当前没有待处理的此类租约。这一步仅读本地账本。
2. 取返回的单一租约编号和 `heartbeat`，在香港报表机运行 `python3 observe_worker.py LEASE HEARTBEAT`。将 JSON 通过已有的管理连接存入 CPU 的 root 私有文件，权限为 0600。
3. 在 CPU 运行 `python3 collect_preauth.py --worker PRIVATE_FILE`。它依次核对集群所有成员、可疑登录、看门狗身份及两端连接。一次检查最多借用原有额度执行一次限定 1.5 秒的会话元数据查询，队列有等待时退出。成功会保存原账本行、观测凭证并返回凭证文件路径。出现仍在连接、会话列表截断、证据过时或见证会话与只读实例不匹配时，保留原名额。
4. 仅当第三步返回 `verified`，在凭证有效期间运行 `python3 reconcile_preauth.py /mnt/data-disk/gpu-backend-offload-20261123/sql-teal-state/remote-leases.sqlite3 PROOF_FILE --apply`。保存返回的摘要。用同一份凭证再次执行只会返回 `already_closed`。
5. 等原定时器触发，验收当天香港快照及 CPU 镜像的数据版本、实际数据、权限和数据库负载。业务回退继续使用原有路由与上一成功快照，已核对结束的会话保持关闭。
