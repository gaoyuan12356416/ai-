# 部署文档

## 变更内容

将 FB 模板入口改为列表页，并新增独立创建/编辑页及共享静态资源。

## 配置项

无配置变更；`FB_AUTO_POST_LIVE_ENABLED` 保持当前值，不因本次 UI 发布调整。

## 数据库变更

无。

## 部署步骤

1. 完成本地测试，提交并推送 `codex/fb-auto-template-list-first-20260820`，记录精确 SHA。
2. 在 `43.166.187.96` 备份以下生产静态文件到数据盘时间戳目录：
   - `/root/drama_material_service/static/fb-auto-publish-templates.html`
   - `/usr/share/nginx/html/fb-auto-publish-templates.html`
3. 从 GitHub 精确 SHA 导出以下文件，校验 SHA-256 后同步到两个静态目录：
   - `fb-auto-publish-templates.html`
   - `fb-auto-publish-template.html`
   - `fb-auto-publish.css`
   - `fb-auto-publish-common.js`
   - `fb-auto-publish-templates.js`
   - `fb-auto-publish-template.js`
4. 不重启 `fb-auto-post-service.service` 或主 API；纯静态文件发布无需 Nginx reload。

## 生产发布记录（2026-08-20）

- 分支：`codex/fb-auto-template-list-first-20260820`。
- GitHub 精确提交：`bda9e7f347d8cd81743f26b65ee4f3e128504e4e`。
- 不可变副本：`/opt/fb-auto-post-ui/releases/bda9e7f347d8cd81743f26b65ee4f3e128504e4e`。
- 发布前备份：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T091220Z-template-list-first-pre-bda9e7f`。
- 旧入口 SHA-256：`416358671bfff16a905dff3e470fdeae2deb7f8a2704934026dfdefd12738210`。
- 发布目标：`/root/drama_material_service/static` 与 `/usr/share/nginx/html`；只写入上述六个静态文件。
- 未重启 `fb-auto-post-service.service` 或主 API，未执行 Nginx reload。

| 文件 | SHA-256 |
| --- | --- |
| `fb-auto-publish-templates.html` | `89e243631875c955e831d5dec8e9fe5f9c6b3f02aa90b371e98edbc49c5160c7` |
| `fb-auto-publish-template.html` | `ac8f2447d15dd69610a5033887fe959049e247d7ca31166eb411661e5f75d50e` |
| `fb-auto-publish.css` | `878dc86d48a3631b53cb3d5ba940db5b8a8301380ac33bf2766e0843fc45fd8b` |
| `fb-auto-publish-common.js` | `3caf225f301796d8134a3620ab2e3272d76f8610b67a5703f409f16a6882d24e` |
| `fb-auto-publish-templates.js` | `501c3cf6929fd4acb1f67e0369872f4a04632d03d3b4d0c843617f4f5ef9fbf2` |
| `fb-auto-publish-template.js` | `639414c3c825b84b5b29b4f50bd1e7f9a9e1e7cf0cd5d991ed3a562d2e9b680b` |

## 验证步骤

- 六个公开静态 URL 均 HTTP 200，源文件/两个生产目录/公网响应 SHA 一致。
- 入口 HTML 包含独立创建链接且不包含 `id="templateForm"`。
- 创建页 HTML 包含 `id="templateForm"` 与返回列表链接。
- 已登录浏览器只读验证列表首屏和创建页布局，不保存生产模板。
- `curl http://127.0.0.1:18835/health` 的 `live_enabled` 不变。
- 发布前后模板/run/task/due-slot/attempt/ledger 计数不变。

实际结果：六个公网 URL 均为 HTTP 200 且与 release/两个生产目录哈希一致；列表与表单静态 DOM 契约通过，匿名浏览器登录门禁正常且控制台无错误。浏览器没有生产登录态，因此未伪造 Cookie；登录态动态交互使用同一提交的本地 mock API 完成验收。发布后 `live_enabled=false`，sidecar `MainPID=3083645`、`NRestarts=0`，六张运行表均为 0，SQLite `quick_check=ok`。

## 回滚方案

1. 将列表 HTML 从备份恢复到两个静态目录。
2. 新增静态文件可保留为未引用文件；如需移除，先确认路径白名单后单文件移至同一备份目录，不递归删除。
3. 复核公网入口恢复旧 SHA；不回滚 SQLite，不重启 FB sidecar。

本次回滚源为：

- 应用目录：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T091220Z-template-list-first-pre-bda9e7f/root-static/fb-auto-publish-templates.html`。
- Nginx 目录：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T091220Z-template-list-first-pre-bda9e7f/nginx-static/fb-auto-publish-templates.html`。

## 注意事项

- 生产路径不是 Git 工作树；必须从已推送的精确提交导出文件，不能直接上传未提交工作区。
- 本次验收不创建模板、不调用 run-now、不产生真实 Graph Post。

## 旧合并页缓存修复发布步骤

1. 提交并推送版本化导航、两张模板页、脚本、`navigation.json` 与 `deploy/nginx-fb-auto-publish.conf`，记录精确 GitHub SHA。
2. 备份两个生产静态目录中的待覆盖文件、两个生产 `navigation.json`、`quick-nav.js`，以及现有 `/etc/nginx/default.d/fb-auto-publish.conf`（若不存在则记录 absent）。
3. 从精确 release 安装变更文件；将 Nginx 配置安装为 `/etc/nginx/default.d/fb-auto-publish.conf`。
4. 先执行 `nginx -t`；仅在通过后执行 `systemctl reload nginx`。无需重启主 API 或 FB sidecar。
5. 验证版本化导航指向纯列表页，列表无 `templateForm`，创建按钮进入独立表单页；三个 HTML 响应均含 `Cache-Control: no-store`。
6. 复核 `live_enabled=false`、sidecar PID/重启次数、六张运行表计数及 SQLite `quick_check` 与发布前一致。

回滚：恢复备份的静态/导航文件；若 Nginx 配置发布前不存在，则将该单文件移入备份目录，若存在则恢复原文件；执行 `nginx -t` 后 reload。不得回滚 SQLite。

## 旧合并页缓存修复生产记录（2026-08-20）

- 分支：`codex/fb-auto-template-list-first-20260820`。
- GitHub 精确提交：`490e3b78cd418e0114e2abba7b097653f18e47b0`。
- 不可变副本：`/opt/fb-auto-post-ui/releases/490e3b78cd418e0114e2abba7b097653f18e47b0`。
- 发布前备份：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T094706Z-stale-shell-cache-pre-490e3b7`。
- 发布前 `/etc/nginx/default.d/fb-auto-publish.conf` 不存在，已在备份清单记录 absent。
- 两个生产 `navigation.json` 的内容范围不同；未覆盖 Nginx 运行时定制导航，仅原子替换其中唯一的 FB 模板 href 为 `/fb-auto-publish-templates.html?v=20260820-list-only-v2`，其余条目保持不变。
- `quick-nav.js`、列表 HTML/JS、表单 HTML/JS 原子安装到应用和 Nginx 两个静态目录；安装精确 Nginx location 后 `nginx -t` 通过并 reload。未重启主 API 或 `fb-auto-post-service.service`。

| 文件 | SHA-256 |
| --- | --- |
| `quick-nav.js` | `efff12a34a8384dbc0cf3c7aa8b09ecead05519d86926aa18c57e9b7d63e3e91` |
| `fb-auto-publish-templates.html` | `2b1c03b0e2b0e2bfe2e8d37061b28d93943056350036c9086119938008204135` |
| `fb-auto-publish-templates.js` | `8144b70cf82d7294c9d11fa30df52e5c5ee05098d31705b012ffab0a09fefc21` |
| `fb-auto-publish-template.html` | `96ec9c9650d56f0aeb6ba80897dc69619999a8ff19add00da1c59faaef3c5868` |
| `fb-auto-publish-template.js` | `463853ce27f222e7d511f9fd71c0b27a174141f758cb36bfa0edd258ee752b0b` |
| `nginx-fb-auto-publish.conf` | `433a36b1293008ed561af2c269a6a793873a0a9e9cf3965bf29df03bfb638f04` |

生产验收结果：

- 无参数及版本化的列表、表单、记录 HTML 均为 HTTP 200，响应包含 `Cache-Control: no-cache, no-store, must-revalidate, max-age=0`、`Pragma: no-cache`、`Expires: 0`。
- 公网导航与 QuickNav 均指向版本化列表；列表无 `templateForm` 且创建按钮进入独立表单，编辑、返回和保存成功跳转契约均通过；匿名模板 API 为 401。
- `fb-auto-publish.css`、公共脚本、两张模板页、两张业务脚本、QuickNav 和记录页公网请求均为 HTTP 200；变更文件公网 SHA 与 release/两个生产静态目录一致。
- 发布后 `live_enabled=false`；FB sidecar `MainPID=3083645`、Nginx `MainPID=2164`，二者 `NRestarts=0` 且 `active/running`。
- `fb_auto_template`、`fb_auto_run`、`fb_auto_task`、`fb_auto_due_slot`、`fb_auto_publish_attempt`、`fb_auto_publish_ledger` 均为 0，SQLite `quick_check=ok`。
- 未创建/修改/启停模板，未调用 run-now，未产生 Graph Post。

本次缓存修复回滚源为：`/mnt/data-disk/fb-auto-post-deploy/backups/20260820T094706Z-stale-shell-cache-pre-490e3b7`。恢复备份静态/导航文件，并因发布前 Nginx 配置不存在而移走该精确配置文件；`nginx -t` 通过后 reload。不得回滚 SQLite。
