# 部署文档

## 变更内容

已实现 X 自动发布模板独立 sidecar/页面/API/定时器，并对现有 X sidecar 增加最小、增量、默认兼容的 `auto_template` 桥接。首次生产部署保持模板为空、三道 gate 全关，不创建真实 Post。

## 配置项

- 新服务非敏感配置独立放入 root-owned `/etc/x-auto-post.env`；bearer 与只读数据库凭据仅放入 `root:x-post-daily`、`0440` 的 `/etc/x-auto-post.secrets`。
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
7. 再安装独立 x_auto sidecar、主 API 代理、静态页和 systemd 单元；三道 gate 保持 0，启动 sidecar 健康检查。静态文件必须逐项读取 `deploy/x-auto-post-static-files.txt` 安装到 `/root/drama_material_service/static/` 和 `/usr/share/nginx/html/`，不得用 `x-auto-publish-*` glob 代替，因为该 glob 不会匹配 `x-auto-publish.css`。
8. 共享 flock 目录只能由 `deploy/x-post-runtime-tmpfiles.conf` 持久管理。暂停相关 timer/path，安装到 `/etc/tmpfiles.d/x-post-runtime.conf` 并执行 `systemd-tmpfiles --create /etc/tmpfiles.d/x-post-runtime.conf`；同批安装 X auto 四个 service/path 与既有 X daily/manual/schedule/catchup unit，均不得保留 `RuntimeDirectory=`。停止仍加载旧 unit 的 sidecar 后再次执行 tmpfiles create，再启动 sidecar并恢复 timer/path。
9. 将 `deploy/x-auto-post-nginx.conf` 安装为 `/etc/nginx/default.d/x-auto-post.conf`，先执行 `nginx -t`，通过后仅 reload Nginx；三个 HTML shell 必须返回 `Cache-Control: no-store, max-age=0`，自身 CSS/JS URL 必须带统一 cache-buster。
10. 重启仅 `drama-material-api.service`；除上述新精确 location 外，不修改其他 Nginx 配置。
11. 启用新 scheduler/runner timer 后只观察自然 `held=live_gates_closed`/`no_pending`；metric timer 可在运营启用模板前再启用。禁止手工运行模板或创建模板作为 canary。

## 验证步骤

- loopback/public health 200；新页面及其 `x-auto-publish.css` 均为 200，CSS 响应必须是 `text/css` 且与不可变 release 同 SHA256；管理 API 响应 `no-store`，未登录接口 401。
- 新 SQLite 模板数 0，所有 live gates 0。
- 现有 X SQLite `integrity_check=ok`；迁移前后旧行摘要一致。
- 现有 material/drama/manual/schedule timer 均保持原状态和下一触发。
- 自然新 timer 返回 no_due/no_pending；现有 queue/log/confirmed Post/unknown/active 计数不变。
- sidecar/main/Nginx 日志无 token、bearer 或异常。
- Linux 实际持有 `/run/x-post-daily/runner.lock` 时，x_auto execute 应无执行；该测试只验证锁，不调用发布 API。
- 记录 `/run/x-post-daily` 与 `/run/x-auto-post` inode；至少两轮既有/X auto 自然 oneshot 后目录仍存在且 inode 不变，证明停止某个 unit 不会重建共享锁路径。

## 回滚方案

1. 停用并停止新 X auto timer/sidecar。
2. 先确认不存在 auto queued/running/unknown row；若存在，保留 forward-compatible X sidecar，仅回滚新页面/主 API/x_auto 单元并完成对账。
3. 无 auto 活跃事实时，将 main API、静态页和新 units 切回部署前 hash，只重启受影响服务；X sidecar 的增量 schema/来源兼容代码可以安全保留。`x-post-runtime.conf` 与既有 X units 去除 `RuntimeDirectory=` 的修复属于共享锁安全基线，不随 X auto 功能回滚而撤销。
4. 保留现有 X SQLite、queue/log/Post、token 和新 X auto SQLite；不得恢复数据库备份覆盖当前发布事实。
5. 不恢复已轮换 token，不删除桥接队列，不清除 canonical 素材占用。
6. 复核健康、原 timer、页面和账本完整性。

## 注意事项

- 生产部署后在本节补充 commit、release、backup、PID、命令结果和精确回滚命令。
- 首次发布只交付关闭状态的能力；启用任一模板需要用户另行明确授权。

## 生产证据（2026-08-11）

- Git commit / release：`0e03210cd2c5c80b134884f9e96304797efa2545`，不可变 release 为 `/mnt/data-disk/x-post-automation/releases/0e03210cd2c5c80b134884f9e96304797efa2545`；部署前 X release 为 `db67fc71a73702feaa88689273356f1a21883bdc`。
- 备份目录：`/mnt/data-disk/x-post-automation/backups/20260811T200055+0800-x-auto-templates-8b4b1b0`；最终 `manifest.sha256` 文件自身 SHA256 为 `9c7983d923f31830b585b4632c1f5d6fdb2bf9025cdc880b00ee19cb80e5aca3`。
- 迁移副本和生产 SQLite 均为 `quick_check=ok`；旧表旧列内容哈希无变化。生产账本保持 queue/log `177/177`、published `176`、failed `1`、confirmed Post ID `176`、active `0`、unknown `0`；四条历史 manual run 均保持 `trigger_source=manual`。账号 token 目录 hash/owner/mode 与部署前一致。
- 新 sidecar 健康；`live_enabled/account_audit_approved/url_property_verified/is_open` 全为 `false`。新 SQLite 的 template/run/task/material-ledger/event 均为 `0`。
- `x-auto-post-scheduler.timer` 与 `x-auto-post-runner.timer` 在 `20:10 CST` 自然执行成功且没有新增任何自动发布事实；现有 `x-post-schedule` 持续 `no_due`，`x-post-manual` 持续 `no_pending`，旧账本计数不变。
- Linux 共享锁实测：root 持有 `/run/x-post-daily/runner.lock` 时，以故意错误的 auto bearer 运行 execute 仍安全返回 0，证明没有访问 sidecar、更没有进入发布路径。
- 已启用新 sidecar、scheduler timer、runner timer 与 runner path；`x-auto-post-metric.timer` 保持 disabled/inactive。未创建模板、未开启 gate、未调用真实 X 发布。
- 页面：`/x-auto-publish-templates.html`、`/x-auto-publish-template.html`、`/x-auto-publish-runs.html` 均返回 200；未登录管理 API 返回 401。

## Chrome 页面验收与静态文件补正（2026-08-12）

- 使用已登录生产 Chrome 会话实际打开模板页和运行记录页时，发现 `/x-auto-publish.css` 返回 404；HTML/API/登录权限本身正常，但页面无样式，且 `.hidden` 失效造成登录与无权限提示错误显示。
- 根因是首次静态部署使用 `x-auto-publish-*` glob；该模式能匹配三个页面及其 JS，却不会匹配点号紧跟在 `publish` 后面的 `x-auto-publish.css`。
- 已从当前不可变 release `0e03210cd2c5c80b134884f9e96304797efa2545` 精确安装同一份 CSS 到应用 static 与 Nginx docroot。补正备份为 `/mnt/data-disk/x-post-automation/backups/20260812T100843+0800-x-auto-css-missing-0e03210cd2c5c80b134884f9e96304797efa2545`，其 manifest 文件 SHA256 为 `40411dcc5dcbff185f0440bfad426a57fa20dea4ae06de22063a7dd18c253f17`。
- 补正后公开 CSS 为 200、`text/css`、14750 字节，与 release SHA256 一致；`nginx -t` 通过，未 reload Nginx、未重启任何服务、未创建模板或运行、未开启 gate、未触发 X Post。
- Chrome 复测确认两个列表页样式加载、登录/权限提示隐藏、空状态与统计正确，刷新、查询、筛选、重置正常；新建模板页可读取 15 个账号快照，其中 5 个当前可发布，固定/随机发布时间切换正常，但未保存模板。

### 精确回滚点

1. 先验证新 SQLite 没有 queued/running/unknown 事实，再执行 `systemctl disable --now x-auto-post-scheduler.timer x-auto-post-runner.timer x-auto-post-runner.path` 和 `systemctl disable --now x-auto-post-service.service`。
2. 主 API、主静态文件和公开静态文件均从上述备份目录的 `files/` 对应路径恢复；移除 `80-x-auto-post.conf` 后仅重启 `drama-material-api.service`。保留新 SQLite，不用备份覆盖任何当前账本。
3. 现有 X sidecar 的 forward-compatible schema/来源隔离代码默认保留。只有在确认 auto run/task/queue/unknown 全为 0 时，才可把 `/opt/x-post-automation/current` 原子切回 `db67fc71a73702feaa88689273356f1a21883bdc` 并仅重启 `x-post-automation.service`。
