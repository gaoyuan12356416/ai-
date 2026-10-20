# 架构与代码评审

已独立检查前后端合同、当前身份broker、定点main API patch及上线/回退边界。实施成员交叉审查，不以单元测试全绿代替审查。

- schema2逐素材时间完整映射；旧协议单独解析，已有request_json/时间/interval不重写。
- Page创建人来自服务端actor；更新保留created_by，历史姓名查询按tenant限定。
- slot永久唯一键不含config实例；source读取在锁外，锁内复核版本、队首、重复事实及额度；SAVEPOINT保护完整冻结。
- 身份broker仅loopback、内部token、无转发头；账号、模块、入口和归因映射每次重读。
- pool共享视图隐藏他人task/batch标识和错误细节，任务详情继续原有owner/admin权限。
- 队列版本变化后游标须重置；见BUG-001。
- 备份SHA、旧列旧行hash与迁移失败边界见BUG-002。不能降级数据库或恢复旧core覆盖新任务执行器。

最终代码评审：通过。新增43项领域、19项身份、9项部署及127项浏览器检查通过；旧core41、source30、gateway9、broker18、自动/旧手动68项通过。真实环境只读验收随部署完成记录。
