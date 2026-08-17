# 部署文档

## 变更内容

部署 X Auto 选择器的剧黑名单豁免；素材黑名单和其他门槛保持不变。

## 配置项

无。

## 数据库变更

无。不得回滚或覆盖生产 SQLite、Token 或黑名单数据。

## 部署步骤

1. 推送精确 GitHub 提交。
2. 在线备份两套 SQLite，记录 Token hash/mode/owner 和当前服务/计时器状态。
3. 从 GitHub 精确提交构建不可变 release。
4. 服务器执行编译和聚焦测试。
5. 暂停相关 X Auto 触发器，原子切换 `current`，仅重启 `x-auto-post-service.service`。
6. 恢复原计时器状态。

## 验证步骤

- 健康接口 `ok=true` 且 gates 保持原值。
- 精确 release 聚焦测试通过。
- 使用 mock/离线 selector 证明剧黑名单被忽略、素材黑名单仍生效。
- SQLite `quick_check=ok`、外键违规为 0。
- Queue/Log/Post/unknown 和 Token hash 在部署前后保持预期；不执行 run-now。

## 回滚方案

停止 X Auto scheduler/runner timer，原子切回部署前 release，重启
`x-auto-post-service.service`，恢复原 timer 状态。保留当前 SQLite 和 Token。

## 注意事项

不得用真实 X Post 验证；不得恢复旧 Token 或旧数据库覆盖新事实。
