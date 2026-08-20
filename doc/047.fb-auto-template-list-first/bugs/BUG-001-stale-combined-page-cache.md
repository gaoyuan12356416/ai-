# BUG-001：浏览器恢复旧合并版模板页

## 状态

已修复并通过生产发布验收，缺陷关闭。

## 现象

2026-08-20 17:34 +08:00 的运营截图中，`FB Page 自动发布模板` 入口仍同时显示“创建模板”大表单和底部模板列表；预期入口只显示列表与“创建模板”按钮。

## 根因

- 截图内容与提交 `bda9e7f` 之前的合并版 HTML 一致。
- 服务器应用静态目录、Nginx 文档根和公网无查询参数响应都已是纯列表页，SHA-256 为 `89e243631875c955e831d5dec8e9fe5f9c6b3f02aa90b371e98edbc49c5160c7`。
- 公网 HTML 响应只有 ETag/Last-Modified，没有 `Cache-Control`；浏览器可继续复用旧文档。

## 修复

1. 导航、创建/编辑入口和保存成功返回统一使用 `v=20260820-list-only-v2`。
2. 两张 FB 模板页加载版本化 QuickNav、CSS 和业务脚本。
3. 新增 `deploy/nginx-fb-auto-publish.conf`，对列表、创建/编辑和记录 HTML 返回 `no-cache, no-store, must-revalidate, max-age=0`。
4. 增加契约测试，禁止列表页出现 `templateForm`，并校验版本化入口与 Nginx 缓存头配置。

## 影响边界

只涉及导航/静态 HTML/JS/CSS 与 Nginx 响应头；不修改 API、数据库、模板数据、发布队列或 Graph 写入。

## 验证

- `python -m unittest scripts.test_fb_auto_app_contract`：6/6 通过。
- `python -m unittest discover -s scripts -p "test_fb_auto_*.py"`：81/81 通过。
- X/TT 主契约回归：66/66 通过。
- 本地 Playwright：版本化导航显示纯模板列表，无 `templateForm`；点击“创建模板”进入独立创建页；控制台 0 error/0 warning。
- 修复提交：`490e3b78cd418e0114e2abba7b097653f18e47b0`；生产 release 与提交一致。
- 生产 `nginx -t` 通过并完成 reload；无参数及版本化的列表、表单、记录 HTML 均返回 `no-store`，列表页无 `templateForm`，创建、编辑、保存返回链路全部指向版本化独立页面。
- 发布后 `live_enabled=false`；FB sidecar `MainPID=3083645`、`NRestarts=0`，六张运行表均为 0，SQLite `quick_check=ok`；未执行任何生产模板或 Graph 写操作。
