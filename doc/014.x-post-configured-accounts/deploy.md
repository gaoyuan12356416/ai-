# 014.x-post-configured-accounts 部署文档

## 变更内容

- daily/sidecar/store 从固定 3 账号改为配置数量 N（1 至 50）。
- X 账号列表 DTO 和管理员页面增加逐账号自动发布配置状态。
- 本次生产 daily 账号行 ID 从 3 个扩为 9 个。
- 当天已有计划保持冻结，不补发新增 6 个账号。

## 配置项

以下两份 root-only 文件必须写入完全相同、顺序一致的值：

```text
/etc/x-post-automation.env
X_POST_DAILY_ACCOUNT_IDS=2,3,4,5,6,7,8,9,10

/etc/x-post-daily.env
X_POST_DAILY_ACCOUNT_IDS=2,3,4,5,6,7,8,9,10
X_POST_DAILY_MAX_REPAIRS_PER_RUN=9
```

- sidecar env 保持 root 所有、`0600`；daily env 保持 root 所有、`0400`。
- daily oneshot 使用 `TimeoutStartSec=360min`，为 9 次 GPU 修复和 9 次顺序发布保留完整窗口。
- 不在命令输出、文档、日志或提交中打印其他 env 值、真实用户名和 Token。
- 更新后必须分别按配置解析规则读取并比较 ID、顺序、唯一性和数量，结果应为同一组 9 个 ID。

## 数据库变更

无。不得为了扩容重写、删除或补齐历史 run/queue/log；SQLite 仍需在部署前在线备份并验证 `PRAGMA integrity_check=ok`。

## 部署前门禁

1. 只读确认当前 release、GitHub 基线、sidecar/main API/timer 状态和下次触发时间。
2. 记录当天 run 的冻结账号数、queue/log/Post 计数及 unknown 状态，作为“零补发”基线。
3. 确认 9 个目标账号均存在且为 active/publishable；不读取或输出 Token 正文。
4. 确认素材池至少准备 9 条可用素材；不足不构成放宽预检的理由。
5. 在线备份 SQLite，并备份两份 env、受影响 unit/static/code 和当前 release 指针；记录 hash/mode。
6. 本地 X 全套测试、Python 编译、页面契约和 `git diff --check` 通过；精确 commit 已推送 GitHub。

## 部署步骤

1. 若临近 timer 窗口，先停用 timer，防止配置双文件更新期间触发。
2. 从已推送的精确 GitHub commit 构建 immutable release。
3. 使用临时文件和原子替换更新两份 env 的唯一目标键，保留其他配置及正确权限。
4. 在不输出凭证的前提下解析比对两份有序 ID 列表，确认均为 9 个且完全一致。
5. 切换 release，安装受影响代码/static/unit；执行 `daemon-reload`。
6. 重启 X sidecar；如主后台文件或静态页面有变化，仅重启受影响的 main API。
7. 验证 health、管理员账号列表和 9 行 `daily_auto_publish_configured=true`。
8. 恢复/启用 timer，但不得执行 `systemctl start x-post-daily.service`；确认下次触发是下一次自然窗口。

## 验证步骤

- sidecar/main API/timer 均正常，daily oneshot 未被手工启动。
- 两份 env 的账号 ID 和顺序一致，文件 ownership/mode 正确。
- 管理员账号列表中目标 9 个账号均显示“已配置”，非目标账号显示“未配置”。
- 匿名/非管理员访问合同、no-store 和 DTO 脱敏保持正常。
- 部署前后当天 run/queue/log/Post 数量完全一致；原三账号计划仍为原三条，没有新增六条。
- SQLite `integrity_check=ok`，素材重复、账号日重复和 unknown 状态没有异常新增。
- 下一自然日无既有计划时，系统按 9 账号执行全批预检；不足 9 个合格素材时应零 queue、零 Post。
- 首个九账号自然批次结束后，核对 9 个账号对应的 queue/log/Post、预览链接和 timer 下一触发时间。

## 回滚方案

1. 先停用 daily timer/service，阻止新批次。
2. 保存当前 SQLite、日志、短链和 Token 状态；如已产生任何真实 queue/Post，严禁用部署前 SQLite 覆盖。
3. 切回上一精确 release，并恢复备份的两份 env，使其继续保持彼此一致。
4. `daemon-reload` 后仅重启受影响 sidecar/main API，再恢复 timer。
5. 保留新字段的客户端兼容与全部历史审计，不删除 queue/log，也不清理素材绑定。
6. 验证服务 health、账号配置状态、SQLite 完整性和下一自然触发。

## 注意事项

- 本部署授权配置扩容，不授权当天补发或手工测试 Post。
- “已配置”不等于“账号健康”；任一目标账号不可发布时，下一自然批次应全批预检失败。
- 生产部署 commit、release、备份路径、部署前后计数及首个九账号自然批次结果待执行后补充。
