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

## 验证步骤

- 六个公开静态 URL 均 HTTP 200，源文件/两个生产目录/公网响应 SHA 一致。
- 入口 HTML 包含独立创建链接且不包含 `id="templateForm"`。
- 创建页 HTML 包含 `id="templateForm"` 与返回列表链接。
- 已登录浏览器只读验证列表首屏和创建页布局，不保存生产模板。
- `curl http://127.0.0.1:18835/health` 的 `live_enabled` 不变。
- 发布前后模板/run/task/due-slot/attempt/ledger 计数不变。

## 回滚方案

1. 将列表 HTML 从备份恢复到两个静态目录。
2. 新增静态文件可保留为未引用文件；如需移除，先确认路径白名单后单文件移至同一备份目录，不递归删除。
3. 复核公网入口恢复旧 SHA；不回滚 SQLite，不重启 FB sidecar。

## 注意事项

- 生产路径不是 Git 工作树；必须从已推送的精确提交导出文件，不能直接上传未提交工作区。
- 本次验收不创建模板、不调用 run-now、不产生真实 Graph Post。
