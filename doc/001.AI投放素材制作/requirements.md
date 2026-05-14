# 001.AI投放素材制作 需求与技术设计

## 背景

AI 后台已有剧集合成、封面/截图素材等任务管理能力。本需求新增“投放素材任务管理”功能，用于承接优化师在后台创建广告投放素材制作任务，并串联需求生成、需求审核、AI/GPU 素材生成、素材审核、素材上报到素材库的完整流程。

流程来源为用户提供的泳道图和 `api-post-material-source.md`。最终素材上报接口为 `POST https://aa.yingliangads.com/api/material/source`，Bearer token 只能作为服务端密钥配置，不允许进入代码仓库、前端、文档明文或 GitHub 提交。

## 目标

- 在 AI 后台快速导航栏新增“投放素材任务管理”入口。
- 支持创建、编辑、发布、复制、删除投放素材任务。
- 支持三类任务：素材优化、竞品借鉴、综合策划；后续任务类型可配置扩展。
- 按状态流转完成：待发布、生成需求中、需求待审核、需求打回、生成素材中、素材待审核、素材打回、已完成。
- 支持需求人自行审核需求和素材；驳回必须填写原因，并驱动后续重新生成。
- 素材审核全部通过后，逐条调用自定义素材 API 上报。
- 管理员可见全部任务，普通授权用户只能看自己的任务。
- 管理员可在用户管理中授权其他用户使用本页面。

## 范围

### 包含

- 后台页面、快速导航入口和权限控制。
- 服务端任务/素材/审核记录持久化。
- 产品下拉、国家/语言输入匹配、任务类型、竞品查询源等表单能力。
- 上传素材参考文件并保存到服务端/COS 可访问路径。
- 需求生成和素材生成的异步任务状态机。
- 飞书消息通知需求人审核。
- 素材上报 API 适配和单条素材逐条提交。
- 文档、测试用例、部署说明和 GitHub 分支维护。

### 不包含

- 重新设计现有 AI 后台登录体系。
- 改造第三方竞品数据源的核心算法。
- 将 Bearer token 写入代码或提交到 GitHub。
- 保留需求历史版本。驳回后只保留最新需求内容。

## 用户故事 / 业务规则

### 角色

- 管理员：admin 角色，能看到全部投放素材任务，并能给其他用户授权页面权限。
- 需求人/优化师：通过飞书登录，邮箱匹配 `admin_user_group.email` 定位用户，只能看到自己的任务；需求审核人就是需求人本人。

### 任务创建

必填字段：

- 产品：来自 CPU 服务器业务库。产品数据基于 `ads_apps_setting`，并通过 `admin_role_apps`、`admin_role_users` 限制到当前登录用户有权限的产品。
- 任务类型：素材优化、竞品借鉴、综合策划。
- 任务数量：1 到 20，不能为空，不能为 0。
- 国家：通用国家缩写输入框，支持相似查询。
- 语言：通用语言缩写输入框，支持相似查询。

可选字段：

- 标签：自定义输入，用于最终上报 `tag_name`。
- 尺寸：自定义输入，用于约束最终素材生成。
- category：自定义输入，用于最终上报 `category`。
- title：自定义输入，用于最终上报 `title`。
- body：自定义输入，用于最终上报 `body`。
- 素材参考：上传文件。
- 任务描述：自由文本。
- 竞品查询接口：仅在任务类型为“竞品借鉴”或“综合策划”时展示，枚举为有米云、metapi、广大大，默认有米云。

### 任务操作限制

- 发布前：允许编辑、删除、复制。
- 发布后：不允许编辑，允许复制。
- 完成前：允许删除。
- 已完成：不允许删除。
- 管理员可看全部任务；普通用户只看自己的任务。

### 状态流转

| 状态 | 触发 | 说明 |
| --- | --- | --- |
| 待发布 | 创建任务 | 用户可继续编辑任务 |
| 生成需求中 | 点击发布、需求驳回后重新生成 | 后端调用需求生成逻辑 |
| 需求待审核 | 需求生成完成 | 飞书通知需求人审核 |
| 需求打回 | 需求审核不通过 | 驳回原因必填，不保留历史版本 |
| 生成素材中 | 需求审核通过 | 后端调用 AI/GPU 素材生成 |
| 素材待审核 | 素材生成完成 | 页面逐个展示素材本身 |
| 素材打回 | 素材审核不通过 | 只重新生成未通过素材 |
| 已完成 | 全部素材审核通过且上报成功 | 任务归档，禁止删除 |

### 素材审核

- 素材审核页面只需要展示素材本身。
- 支持单条通过/驳回，也支持批量选择。
- 驳回必须填写原因。
- 不通过的素材按原因重新生成；已通过素材不重复生成。
- 全部素材通过后，每条素材单独调用一次最终上报 API。

### 最终素材上报映射

| 上报字段 | 来源 |
| --- | --- |
| `app_id` | 任务产品 |
| `country` | 任务国家 |
| `language` | 任务语言 |
| `content_sign` | 素材唯一 ID |
| `url` | 素材 COS URL |
| `name` | 素材名称 |
| `user_id` | 固定 `248` |
| `initiator` | 当前登录用户通过 `admin_user_group.email` 定位后的 `sub_user_id` |
| `category` | 任务创建时填写；为空则传空 |
| `tag_name` | 任务创建时填写；为空则传空 |
| `title` | 任务创建时填写；为空则传空 |
| `body` | 任务创建时填写；为空则传空 |
| `remark` | 固定空字符串 |

## 交互与流程

### 页面结构

- 顶部：标题“投放素材任务管理”、创建任务按钮、状态统计。
- 筛选区：任务状态、任务类型、产品、国家、语言、创建人、关键词、日期范围。
- 列表区：ID、任务类型、产品名称、任务数量、国家、语言、尺寸、标签、任务状态、发起人、素材数量、创建时间、操作。
- 详情弹窗：任务基础信息、需求内容、素材列表、审核操作、上报结果。
- 创建/编辑弹窗：按任务创建字段组织表单。

### 关键操作

1. 用户创建任务，状态为待发布。
2. 用户点击发布，后端进入生成需求中。
3. 后端根据任务类型、产品、参考素材、竞品查询源生成需求。
4. 需求生成完成，状态变为需求待审核，并通过飞书通知需求人。
5. 需求人审核需求：
   - 通过：进入生成素材中。
   - 不通过：填写驳回原因，进入需求打回并自动重新生成需求。
6. AI/GPU 生成素材，完成后进入素材待审核。
7. 需求人审核素材：
   - 通过：标记该素材通过。
   - 不通过：填写驳回原因，只重新生成该素材。
8. 全部素材通过后，逐条调用素材上报接口。
9. 所有素材上报成功后，任务状态为已完成。

## 技术设计

### 影响模块

- `app.py`：新增配置、SQLite 表、任务服务、状态机、API 路由、素材上报适配。
- `static/index.html`：新增页面、表单、列表、详情/审核弹窗、前端状态流转操作。
- `.env.example`：新增素材任务相关服务配置项，保留 token 空值。
- `deploy/drama-material-api.service`：按需补充服务环境变量示例。
- `doc/001.AI投放素材制作/`：需求、评审、测试、部署、接口文档。

### 数据结构

新增 SQLite 表，沿用现有 `DRAMA_JOB_DB_PATH` 和 `JOB_DB_LOCK`：

- `ad_material_task`：任务主表。
- `ad_material_asset`：任务下生成的素材记录。

任务主表核心字段：

- `task_id`
- `task_type`
- `competitor_source`
- `app_id`
- `product_name`
- `country`
- `language`
- `size`
- `tag_name`
- `category`
- `title`
- `body`
- `description`
- `quantity`
- `reference_files_json`
- `status`
- `demand_text`
- `review_reason`
- `creator_user_id`
- `creator_open_id`
- `creator_email`
- `creator_name`
- `initiator_sub_user_id`
- `created_at`
- `updated_at`

素材表核心字段：

- `asset_id`
- `task_id`
- `asset_index`
- `name`
- `url`
- `local_path`
- `status`
- `review_reason`
- `source_api_id`
- `source_api_error`
- `created_at`
- `updated_at`

### API / 接口

新增后台接口：

- `GET /api/ad-material/products`：获取当前用户可见产品。
- `GET /api/ad-material/tasks`：任务列表。
- `POST /api/ad-material/tasks`：创建任务。
- `GET /api/ad-material/tasks/{task_id}`：任务详情。
- `POST /api/ad-material/tasks/{task_id}`：编辑待发布任务。
- `POST /api/ad-material/tasks/{task_id}/copy`：复制任务。
- `POST /api/ad-material/tasks/{task_id}/publish`：发布任务。
- `POST /api/ad-material/tasks/{task_id}/demand-review`：需求审核。
- `POST /api/ad-material/tasks/{task_id}/assets/{asset_id}/review`：素材审核。
- `POST /api/ad-material/tasks/{task_id}/complete-upload`：全部通过后逐条上报。
- `DELETE /api/ad-material/tasks/{task_id}`：删除未完成任务。

最终素材上报接口：

- `POST https://aa.yingliangads.com/api/material/source`
- 服务端配置：`AD_MATERIAL_SOURCE_API_URL`、`AD_MATERIAL_SOURCE_API_TOKEN`
- 不在前端暴露 token。

### AI/竞品源/GPU 适配

CPU 服务器已检索到以下 skill：

- `/root/.codex/skills/image-material-requirements`
- `/root/.codex/skills/image-material-requirements-appgrowing`
- `/root/.codex/skills/image-material-requirements-metapi`

GPU 服务器已检索到：

- `/root/codex_adgen_mxc/generate_mxc_ads_from_brief.py`
- 输出 manifest 包含 `outputs[].cos_url`，可作为素材审核和上报 URL。

实现策略：

- 需求生成优先调用可配置命令 `AD_MATERIAL_REQUIREMENT_COMMAND`。
- 素材生成优先调用可配置命令 `AD_MATERIAL_GENERATION_COMMAND`。
- 命令未配置时，生成可审核的结构化需求文本和占位素材记录，保证任务状态机、页面和上报流程可验证。
- 真实上线时通过环境变量接入 CPU skill 和 GPU 生成服务，不把外部密钥写入代码。

### 异常与边界

- 发布后禁止编辑。
- 已完成禁止删除。
- 非 admin 用户只能访问自己的任务。
- 需求/素材驳回必须填写原因。
- 任务数量必须在 1 到 20。
- `user_id` 固定 248，`initiator` 找不到时不允许最终上报。
- 最终上报单条失败时记录错误，任务不进入已完成。
- API token 缺失时禁止真实上报，提示配置缺失。
- 上传文件大小限制需要沿用当前 HTTP 服务能力，前端可先限制单文件 20MB。

## 验收标准

- 管理员能在快速导航进入“投放素材任务管理”。
- 管理员能授权普通用户访问该页面。
- 普通用户只能看到自己创建的任务。
- 创建任务时三类任务类型可选；竞品查询接口只在竞品借鉴/综合策划展示。
- 任务数量 1-20 校验有效。
- 发布前可编辑/删除/复制；发布后不可编辑但可复制；完成前可删除；完成后不可删除。
- 需求驳回和素材驳回原因必填。
- 素材审核只展示素材本身，支持单条审核。
- 全部素材通过后逐条调用最终上报接口。
- token 不出现在前端代码、文档正文、提交 diff 和 GitHub。
- 本地编译/语法检查通过。

## 风险与待确认

- GitHub connector 当前返回认证侧错误，后续如无法通过插件开 PR，需要走本地 git push 或补充 GitHub 授权。
- 真实 CPU/GPU 生成命令需要线上环境变量配置；开发阶段先完成可插拔适配和状态机。
- 产品权限 SQL 需要根据线上表结构最终校准。

## 变更记录

- 2026-05-14：根据流程图、接口文档和用户答复整理完整需求与技术设计。
