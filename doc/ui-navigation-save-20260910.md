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
- 浏览器修改模拟模板名称并点击保存：请求保留修改值和 `expected_version=3`，成功后到模板列表，无未保存拦截。
- 模拟 HTTP 409：留在编辑页，显示错误，保留输入，保存按钮恢复可用，未保存离页保护仍有效。
- 独立只读复核通过。浏览器验证未写入任何线上模板或触发发布。

## 静态部署

基线为 `b4819898ffb06152f21b41b0729503149d1cd925`。部署前相关源文件与公网及两处线上文件一致（排除 Windows CRLF）。只发布：

- `static/quick-nav.js`
- `static/x-auto-publish-template.js`
- `static/x-auto-publish-template.html`

GitHub 推送后由 `43.166.187.96` 拉取确切提交，部署至 `/usr/share/nginx/html/` 和 `/root/drama_material_service/static/`。替换前校验三个文件的旧 SHA-256 并备份两处原文件；先安装 JS，再安装编辑页 HTML。编辑 JS 使用独立缓存版本 `20260910save1`，导航的公网响应已设置 no-store。

本次无需重启 Nginx、主 API 或任何发布服务。回滚时恢复这两处目录的三个原文件，保留数据库和运行中的任务。具体提交、备份位置与回滚命令在部署完成后补录。
