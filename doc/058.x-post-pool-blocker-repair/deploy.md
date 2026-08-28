# 部署与回滚

## 发布门禁
GitHub exact commit；本地/服务器测试通过；在线SQLite backup quick_check=ok且FK=0；令牌仅备份并记录hash/mode不输出内容；媒体/未知记录精确快照。

## 部署
- 主机43.166.187.96。现Sidecar /opt/x-post-automation/current -> /mnt/data-disk/x-post-automation/releases/18d9cfb68ee330db633b769112f0a50e38bc3e7c。
- 独立release从GitHub拉取目标commit，测试后原子切换；仅重启受影响Sidecar和必要主API。
- 主API现service.py与Sidecar存在差异，先比对保留其现行行为，再决定同步范围；不得覆盖未审阅的主API版本。
- /etc/x-post-automation.env显式启用X_POST_DEFERRED_DRAMA_REPAIR_*，GPU凭据从既有本地受控env在进程内复制，绝不进入Git或报告。内部请求timeout应覆盖准备+上传。
- 切换/恢复期间暂停schedule和claim timer；确认无inflight后操作，不中断已发起的X写入；恢复原timer启用状态。
- 不人工启动发布service作测试，只观察自然timer和账本。

## 数据恢复
348的635—647、350的667—669，16条完整原队列。先在线backup并校验所有原source身份/URL、零attempt/无ID/无unknown。按每run独立validate-only、备份副本apply、live apply。保留已有relay路由、queue/log主键、pool绑定和episode；不清q533/719/726。

## 回滚边界
停止后续schedule/claim触发，确认未有在途写入；原子切回旧release并恢复本次env备份，重启Sidecar；如主API同步过则恢复对应已备份代码并窄重启。自然发布已开始后不得整库恢复，不删除队列/发布/恢复审计。确切路径、命令和验证结果在最终部署记录补全。
