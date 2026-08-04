# 部署与回滚

## 部署前

1. 记录生产 release、服务/定时器状态及 TT 数据库路径。
2. 使用 SQLite online backup 备份数据库并执行 `PRAGMA integrity_check`。
3. 导出表/列清单和自动配置、排期、日计划、运行、队列的非敏感计数。

## 发布

1. 本地测试通过后提交并推送 GitHub。
2. 服务端从精确提交构建不可变 release。
3. 在数据库副本演练迁移；成功后切换 release。
4. 只重启 `tt-post-service`；保持 GPU 服务不变。
5. 不手动启动发布 runner，不创建真实任务。

## 验证

- health、systemd service/timer、页面/API、SQLite schema/integrity。
- 用隔离数据库生成固定及随机计划，验证 60 分钟、非整点、账号隔离和重启稳定。
- 对比生产发布 ID、队列和运行基线；部署验证期间不得新增真实发布请求。

## 回滚

- 将 `/opt/tt-post/current` 切回部署前 release，恢复静态页并重启 `tt-post-service`。
- 新表和加法字段可保留；如必须恢复数据库，先停服务后使用部署前 online backup。
