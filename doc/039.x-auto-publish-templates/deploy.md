# 部署文档

## 变更内容

已实现 X 自动发布模板独立 sidecar/页面/API/定时器，并对现有 X sidecar 增加最小、增量、默认兼容的 `auto_template` 桥接。首次生产部署保持模板为空、三道 gate 全关，不创建真实 Post。

## 配置项

- 新服务配置独立放入 root-only `/etc/x-auto-post.env`。
- 三道生产闸门首次部署均为 `0`：
  - `X_AUTO_POST_LIVE_ENABLED`
  - `X_AUTO_POST_ACCOUNT_AUDIT_APPROVED`
  - `X_AUTO_POST_URL_PROPERTY_VERIFIED`
- 为现有 X sidecar 新增独立 `X_POST_AUTO_INTERNAL_TOKEN`，与 backend/daily bearer 两两不同；值只写 root 管理的环境文件，不写日志或文档。
- 新服务的 admin token 与 execution token 分文件注入，浏览器只能访问主 API 代理。

## 数据库变更

- 新 SQLite：`/mnt/data-disk/x-auto-post-publisher/x-auto-post.sqlite3`。
- 现有 `/var/lib/x-post-automation/accounts.sqlite3` 只做增量、幂等列/索引变更。
- 部署前用 SQLite online backup；在副本重复执行迁移并比较旧表行摘要、queue/log/Post 计数和 `integrity_check`。

## 部署步骤

1. 推送并锁定 GitHub commit，构建不可变 release。
2. 记录生产 app/static/modules/units hash、当前 release、PID、timer 和下一触发时间。
3. 备份现有 X SQLite、token 目录（只记录 hash/owner/mode）、相关代码/静态文件/单元并校验 manifest。
4. 在备份副本演练迁移；失败则停止。
5. 先 provision 两个全新 bearer；保持 x_auto 尚未运行且数据库无 auto row。
6. 先部署 forward-compatible 的现有 X sidecar/存储迁移，重启仅 `x-post-automation.service`；验证 manual/daily/schedule/pool API 和 timer 不变。
7. 再安装独立 x_auto sidecar、主 API 代理、静态页和 systemd 单元；三道 gate 保持 0，启动 sidecar 健康检查。
8. 重启仅 `drama-material-api.service`；只有 Nginx 配置实际变化时才 reload Nginx。
9. 启用新 scheduler/runner timer 后只观察自然 `held=live_gates_closed`/`no_pending`；metric timer 可在运营启用模板前再启用。禁止手工运行模板或创建模板作为 canary。

## 验证步骤

- loopback/public health 200；新页面 200/no-store，未登录接口 401。
- 新 SQLite 模板数 0，所有 live gates 0。
- 现有 X SQLite `integrity_check=ok`；迁移前后旧行摘要一致。
- 现有 material/drama/manual/schedule timer 均保持原状态和下一触发。
- 自然新 timer 返回 no_due/no_pending；现有 queue/log/confirmed Post/unknown/active 计数不变。
- sidecar/main/Nginx 日志无 token、bearer 或异常。
- Linux 实际持有 `/run/x-post-daily/runner.lock` 时，x_auto execute 应无执行；该测试只验证锁，不调用发布 API。

## 回滚方案

1. 停用并停止新 X auto timer/sidecar。
2. 先确认不存在 auto queued/running/unknown row；若存在，保留 forward-compatible X sidecar，仅回滚新页面/主 API/x_auto 单元并完成对账。
3. 无 auto 活跃事实时，将 main API、静态页和新 units 切回部署前 hash，只重启受影响服务；X sidecar 的增量 schema/来源兼容代码可以安全保留。
4. 保留现有 X SQLite、queue/log/Post、token 和新 X auto SQLite；不得恢复数据库备份覆盖当前发布事实。
5. 不恢复已轮换 token，不删除桥接队列，不清除 canonical 素材占用。
6. 复核健康、原 timer、页面和账本完整性。

## 注意事项

- 生产部署后在本节补充 commit、release、backup、PID、命令结果和精确回滚命令。
- 首次发布只交付关闭状态的能力；启用任一模板需要用户另行明确授权。

## 生产证据（部署后填写）

- Git commit / release：待部署。
- 备份目录与 manifest SHA256：待部署。
- 既有 X SQLite 迁移前后摘要：待部署。
- 新 x_auto health / gates / template count：待部署。
- 自然 timer 与现有 queue/log/Post 不变证据：待部署。
