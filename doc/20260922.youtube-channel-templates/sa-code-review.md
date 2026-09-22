# 代码审查

2026-09-22，实施者自审，无未解决阻断项。

- SQL 参数化 tenant/channel_id，身份解析仅接受十进制 local_id、限定产品1479，DTO剔除scopes和账号内部ID。
- CAS位于BEGIN IMMEDIATE事务中；返回本次准确写入快照，避免并发后再读导致响应混入别人内容。
- 模板GET失败阻止发布提交；加载中控件和宏按钮不可编辑；异步结果必须匹配草稿与请求对象；后台核验不套用。
- 未改创建任务的payload或operation_id/hash，已有任务不重渲染。
- 导航使用局部插入、在线SQLite备份、文件前置SHA与安装SHA校验；回滚拒绝后续文件漂移，保留模板和发布数据。
- Windows回滚演练发现备份路径拼接不跨平台，已改用relative_to(anchor)并通过演练；Linux路径行为保持一致。
