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

### 实际部署记录

- GitHub 代码提交：`e96ecad3b2af33cc6a53e3154f69e4a36dfff769`。
- 生产 release：`/mnt/data-disk/x-post-automation/releases/e96ecad3b2af33cc6a53e3154f69e4a36dfff769`。
- 备份：`/mnt/data-disk/x-post-automation/backups/20260817T143949+0800-x-auto-ignore-drama-blacklist-e96ecad`。
- 精确 release 聚焦测试：125/125 通过。
- 初次切换因 release 根目录为 `0700` 启动失败；timer 尚未恢复，未产生任务/Post。
  修正为 `0755` 后服务健康通过，详见 `bugs/BUG-001.md`。
- 最终只重启 `x-auto-post-service.service`；scheduler/runner/metric timer 恢复到原有
  enabled/active 状态组合。

## 验证步骤

- 健康接口 `ok=true` 且 gates 保持原值。
- 精确 release 聚焦测试通过。
- 使用 mock/离线 selector 证明剧黑名单被忽略、素材黑名单仍生效。
- SQLite `quick_check=ok`、外键违规为 0。
- Queue/Log/Post/unknown 和 Token hash 在部署前后保持预期；不执行 run-now。

### 实际验证结果

- 健康：`ok=true`，`is_open/live_enabled/account_audit_approved/url_property_verified=true`。
- 生产选择器已无两处剧黑名单判断，素材黑名单两处判断仍存在。
- 两套 SQLite `quick_check=ok`、外键检查无输出。
- X Auto Run/Task/Ledger=`27/65/4`，活动 Run/Task 为 0。
- 主 X Queue/Log/Published/unknown=`387/387/386/0`。
- 部署前后 Token 内容清单 hash 一致：
  `e57c4800d9ff0792002258d2efe1f62cdc403a2ab05849b360dd8096a2d10a2c`。
- 14:42 scheduler/runner 自然轮询成功，未创建新 Run/Task/Post。

## 回滚方案

停止 X Auto scheduler/runner timer，原子切回部署前 release，重启
`x-auto-post-service.service`，恢复原 timer 状态。保留当前 SQLite 和 Token。

本次精确回滚目标：
`/mnt/data-disk/x-post-automation/releases/446ba3b4ea1ee799cfbd6db5f90adc7b48c75894`。

## 注意事项

不得用真实 X Post 验证；不得恢复旧 Token 或旧数据库覆盖新事实。
