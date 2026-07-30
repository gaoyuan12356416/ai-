# 部署文档

## 变更内容

X 账号增加管理员发布许可，并将其接入发布池候选、排期与最终发布闸门。

## 配置项

无新增环境变量。

## 数据库变更

`x_authorized_account` 增加：

```sql
publish_approved INTEGER NOT NULL DEFAULT 0
```

sidecar 启动时幂等迁移。迁移后所有历史账号默认值为 0。

## 部署步骤

1. 合并并推送精确 GitHub 提交。
2. 记录生产挂载点、当前 release、服务状态和文件哈希。
3. 使用 SQLite online backup 备份数据库，并备份 Token 目录和待替换文件。
4. 在备份库演练迁移，验证 `integrity_check`、账号计数、默认值与 Token 哈希。
5. 从 GitHub 精确提交创建不可变 release，切换 sidecar。
6. 更新主服务 `app.py`、client 和静态页面。
7. 依次重启 sidecar 与主服务，检查日志和健康状态。

## 验证步骤

- 数据库列存在，历史账号全部 `publish_approved=0`。
- 两个服务 active，页面静态文件与 GitHub 提交一致。
- 未登录管理员 API 返回 401/403，内部鉴权保持有效。
- 读取账号列表确认 `publish_approved=false`、`publish_eligible=false`。
- 不发送真实 X 帖子。

## 回滚方案

恢复旧 release symlink、主服务及静态文件，恢复 SQLite online backup 后重启两服务。旧代码会忽略新增列；若只回滚代码，可保留该列以减少数据库操作。

## 注意事项

上线后不会自动勾选任何账号。管理员需在账号列表逐个确认；勾选仅控制未来发布，不改变 OAuth 与历史记录。
