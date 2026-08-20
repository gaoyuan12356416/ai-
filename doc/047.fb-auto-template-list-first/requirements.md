# 047.FB 自动发布模板列表优先 需求与技术设计

## 背景

生产入口 `/fb-auto-publish-templates.html` 当前先展示完整“创建模板”表单，模板列表位于页面底部。操作路径与 TT、X 自动发布模板不一致，已有模板的查看、启停和手动执行入口不够直接。

2026-08-20 首次拆页发布后，服务器源文件已经是纯列表页，但运营截图仍显示旧合并页。截图时间晚于发布，旧页面内容与发布前 HTML 完全一致；公网响应缺少 `Cache-Control`，确认浏览器复用了旧文档缓存。

## 目标

1. 进入 FB 自动发布模板入口时默认展示模板列表。
2. 在列表页点击“创建模板”后进入独立创建页。
3. 点击某个模板的“编辑”后进入同一独立表单页并加载该模板。
4. 保持现有 FB 自动发布 API、权限、模板配置、启停和手动执行语义不变。

## 范围

### 包含

- 将 `fb-auto-publish-templates.html` 调整为纯列表页。
- 新增 `fb-auto-publish-template.html` 作为创建/编辑页。
- 抽离 FB 页面公共样式、公共鉴权壳、列表脚本和表单脚本。
- 列表页补齐名称/状态筛选及分页，沿用后端既有 `q/status/limit/offset`。
- 更新前端契约测试与本需求文档。
- 为列表/创建/记录 HTML 增加 Nginx `no-store`，并对导航入口、创建/编辑和保存返回链接使用同一版本查询参数。

### 不包含

- 不修改 `features/fb_auto_posts`、主 API 代理、SQLite、MySQL、Page Token 或 Graph 写入逻辑。
- 不创建生产模板，不启用 live gate，不执行真实 Meta 发帖。
- 不改变 TT、X 自动发布页面与发布任务。

## 用户故事 / 业务规则

- 作为运营，我打开 FB 自动发布模板入口后，第一眼应看到已有模板及其状态。
- 我需要通过明确的“创建模板”按钮进入配置页面。
- 我需要从列表中点击“编辑”进入同一配置页面。
- 新建模板仍默认停用；已启用模板仍须先停用再编辑。
- 启停与手动执行继续要求明确确认，并继续携带 `expected_version`；手动执行继续携带唯一 `operation_id`。

## 交互与流程

```text
导航入口
  -> FB 模板列表
       -> 创建模板 -> 独立创建页 -> 保存 -> 返回列表
       -> 编辑模板 -> 独立编辑页 -> 保存 -> 返回列表
       -> 启用/停用/手动执行 -> 二次确认 -> 原 API
```

## 技术设计

### 影响模块

- `static/fb-auto-publish-templates.html`：列表壳与筛选/分页容器。
- `static/fb-auto-publish-template.html`：创建/编辑表单。
- `static/fb-auto-publish-common.js`：Feishu Cookie 鉴权、权限门禁、共享导航、API 请求、确认框与提示。
- `static/fb-auto-publish-templates.js`：列表、筛选、分页、启停与手动执行。
- `static/fb-auto-publish-template.js`：Page 池加载、详情回填、容量估算与保存。
- `static/fb-auto-publish.css`：两页共用样式。

### 数据结构

无数据结构变更。

### API / 接口

沿用既有接口：

- `GET /api/admin/fb-auto-publish/groups`
- `GET /api/admin/fb-auto-publish/templates`
- `GET /api/admin/fb-auto-publish/templates/{id}`
- `POST /api/admin/fb-auto-publish/templates[/{id}]`
- `POST /api/admin/fb-auto-publish/templates/{id}/{enable,disable,run-now}`

### 异常与边界

- 未登录和无权限时只展示既有门禁，不请求业务数据。
- 详情不存在或不可访问时禁止保存并显示中文错误。
- 列表为空时仍展示“创建模板”按钮。
- 列表请求失败时保留页面壳并显示错误，不误报为空列表。
- 手机端列表可横向滚动，表单降为单列。

## 验收标准

1. 生产入口首屏存在“模板列表”和“创建模板”，不存在 `templateForm`。
2. “创建模板”打开 `/fb-auto-publish-template.html`，表单字段与旧页面配置能力一致。
3. “编辑”打开 `/fb-auto-publish-template.html?id={id}` 并调用模板详情接口回填。
4. 保存调用原创建/更新接口，成功后返回列表。
5. 启停和手动执行仍分别携带版本与操作 ID。
6. 新增/修改 JavaScript 均通过 `node --check`；FB 前端契约测试通过。
7. 生产只发布静态文件，不重启发布 sidecar，不改变 `live_enabled=false`，不产生模板、run、task 或 Graph Post。
8. 导航入口使用版本化列表 URL；列表/创建/记录 HTML 响应包含 `Cache-Control: no-store`，旧合并页不能再由浏览器缓存恢复。

## 风险与待确认

- 仅依赖 HTML ETag 不足以消除已有浏览器缓存；入口使用版本查询参数，三个 HTML 壳由 Nginx 返回 `no-store`。
- 新页面必须同时发布到主服务静态目录与 Nginx 公开目录。
- 上线后使用只读页面/HTTP/哈希/健康检查验收，不保存生产模板。

## 变更记录

- 2026-08-20：根据运营反馈冻结列表优先和独立创建/编辑页范围。
- 2026-08-20：根据运营截图确认旧合并页缓存缺陷，补充版本化入口和 Nginx 禁用 HTML 缓存要求。
