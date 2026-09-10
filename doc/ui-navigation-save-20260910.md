# AI 后台导航与 X 模板保存交互（2026-09-10）

## 行为

- 进入或刷新页面时，快速导航自动展开当前页面所属分组，其余分组默认收起。
- 同页手动展开/收起在导航配置异步加载、权限重渲染时保留；通过 `QuickNav.setActive` 切到另一个页面时展开目标分组。`item.key` 和旧配置的 `item.view` 均可匹配。
- X 自动发布模板编辑/创建请求成功后返回 `/x-auto-publish-templates.html`。跳转前清除未保存标记；校验错误或请求失败保留编辑页及输入。
- API 请求、版本冲突保护、模板启停和发布逻辑不变。

## 验证

- `python scripts/test_x_auto_publish_ui.py`：15 项通过。
- `python scripts/test_x_auto_post_static_deploy.py`：4 项通过。
- 维护技能要求的 8 个 Python 模块通过 `py_compile`；共享导航及 X 页面脚本通过 Node 语法检查。
- 本地 Chromium + localhost 静态服务、模拟 API：首次 X 页只展开 X 分组；手动收起后重渲染仍收起；切换 TT 后再返回 X 可重新展开。
- 同一共享组件分别以短剧、TT、Facebook、设置页面入口初始化：每次只默认展开正确分组，4 项均通过。
- 浏览器修改模拟模板名称并点击保存：请求保留修改值和 `expected_version=3`，成功后到模板列表，无未保存拦截。
- 模拟 HTTP 409：留在编辑页，显示错误，保留输入，保存按钮恢复可用，未保存离页保护仍有效。
- 独立只读复核通过。浏览器验证未写入任何线上模板或触发发布。

## 静态部署

基线为 `b4819898ffb06152f21b41b0729503149d1cd925`。部署前相关源文件与公网及两处线上文件一致（排除 Windows CRLF）。只发布：

- `static/quick-nav.js`
- `static/x-auto-publish-template.js`
- `static/x-auto-publish-template.html`

GitHub 推送后由 `43.166.187.96` 拉取确切提交，部署至 `/usr/share/nginx/html/` 和 `/root/drama_material_service/static/`。替换前校验三个文件的旧 SHA-256 并备份两处原文件；先安装 JS，再安装编辑页 HTML。编辑 JS 使用独立缓存版本 `20260910save1`，导航的公网响应已设置 no-store。

本次无需重启 Nginx、主 API 或任何发布服务。回滚时恢复这两处目录的三个原文件，保留数据库和运行中的任务。

## 已部署记录

- 分支：`codex/ai-ui-navigation-save-20260910`，已推送 GitHub。
- 已部署代码提交：`f9eb949641e28017350e3faba330034b7199ac17`。
- 服务器从 GitHub 拉取并检出到：`/mnt/data-disk/ai-ui/releases/f9eb949641e28017350e3faba330034b7199ac17`。仅同步上述三个静态文件，没有切换整套后端代码。
- 部署前验证数据盘 UUID 和六个目标文件旧哈希。备份：`/mnt/data-disk/ai-ui/backups/20260910-f9eb949`，包含 `nginx/`、`runtime/`、`manifest.json` 和 `rollback.sh`。
- 公网三个资源全部 HTTP 200，字节内容与代码提交一致；服务器两处目标内容也一致。编辑页已引用 `?v=20260910save1`。
- Nginx、主 API 均 active，启动时间与部署前一致；主 API `/api/ui/topbar` 返回 200；最近 10 分钟 Nginx journal 无 error。
- AI backend maintenance 技能已补充新版导航及保存行为，替代旧的进入页面全部收起规则。

在 CPU 服务器执行以下命令即可恢复本次部署前的六个静态文件，无需重启：

```bash
bash /mnt/data-disk/ai-ui/backups/20260910-f9eb949/rollback.sh
```

公网 SHA-256：

| 文件 | SHA-256 |
| --- | --- |
| quick-nav.js | `79908d7664cb88ac46e4ea1c160442c0ebd3b813077cb3913d0e15c825f22cfe` |
| x-auto-publish-template.js | `60be72fae50d47db238a0b47eb9260db0379f1cb258ea27b83472fd272f63a56` |
| x-auto-publish-template.html | `31da6eb779cc9f1bda009b2e4f97e450e09afc77edc0b6b50ec118f61edd105d` |
