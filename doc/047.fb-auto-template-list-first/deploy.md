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
