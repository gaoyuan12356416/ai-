# API 文档

## 后台接口

所有后台接口复用现有飞书登录 Cookie 或后台 token 校验。

### 获取产品

`GET /api/ad-material/products`

返回当前用户可见产品。admin 可返回全部产品，普通用户按 `admin_role_apps`、`admin_role_users` 权限过滤。

### 任务列表

`GET /api/ad-material/tasks`

查询参数：

- `status`
- `task_type`
- `app_id`
- `country`
- `language`
- `q`
- `page`
- `page_size`

普通用户仅返回自己的任务。

### 创建任务

`POST /api/ad-material/tasks`

请求体：

```json
{
  "app_id": "10001",
  "task_type": "素材优化",
  "quantity": 5,
  "country": "MX",
  "language": "es",
  "size": "1080x1080",
  "tag_name": "spring",
  "category": "promo",
  "title": "广告标题",
  "body": "广告正文",
  "description": "任务描述",
  "competitor_source": "有米云",
  "reference_files": [
    {
      "name": "ref.png",
      "content_type": "image/png",
      "data_url": "data:image/png;base64,..."
    }
  ]
}
```

约束：

- `quantity` 为 1 到 20。
- `competitor_source` 仅在竞品借鉴/综合策划时有效。
- 上传文件保存为服务端任务附件，不向前端暴露本地绝对路径。

### 编辑任务

`POST /api/ad-material/tasks/{task_id}`

仅状态为待发布时允许。

### 复制任务

`POST /api/ad-material/tasks/{task_id}/copy`

复制后生成新的待发布任务。

### 发布任务

`POST /api/ad-material/tasks/{task_id}/publish`

将任务置为生成需求中，并异步生成需求。

### 需求审核

`POST /api/ad-material/tasks/{task_id}/demand-review`

请求体：

```json
{
  "result": "approved",
  "reason": ""
}
```

`result` 可选：

- `approved`：进入生成素材中。
- `rejected`：原因必填，覆盖旧需求并重新生成。

### 素材审核

`POST /api/ad-material/tasks/{task_id}/assets/{asset_id}/review`

请求体：

```json
{
  "result": "approved",
  "reason": ""
}
```

### 完成上传

`POST /api/ad-material/tasks/{task_id}/complete-upload`

全部素材审核通过后逐条调用最终素材上报接口。

### 删除任务

`DELETE /api/ad-material/tasks/{task_id}`

已完成任务禁止删除。

## 最终素材上报接口

来源：`C:/Users/gaoyu/Downloads/api-post-material-source.md`

- 方法：`POST`
- URL：`https://aa.yingliangads.com/api/material/source`
- Header：`Authorization: Bearer <token>`
- Content-Type：`application/json`

服务端配置：

- `AD_MATERIAL_SOURCE_API_URL`
- `AD_MATERIAL_SOURCE_API_TOKEN`
- `AD_MATERIAL_SOURCE_API_TIMEOUT`

请求体字段：

| 字段 | 必填 | 来源 |
| --- | --- | --- |
| `app_id` | 是 | 任务产品 |
| `country` | 是 | 任务国家 |
| `language` | 是 | 任务语言 |
| `content_sign` | 是 | 素材唯一 ID |
| `url` | 是 | 素材 COS URL |
| `name` | 是 | 素材名称 |
| `user_id` | 是 | 固定 248 |
| `initiator` | 是 | 登录用户映射到 `admin_user_group.sub_user_id` |
| `category` | 否 | 任务 category |
| `tag_name` | 否 | 任务 tag_name |
| `title` | 否 | 任务 title |
| `body` | 否 | 任务 body |
| `remark` | 否 | 固定空字符串 |

响应处理：

- 成功：记录返回 `data.id`。
- `-1`：记录参数或业务校验失败。
- `-2`：记录保存异常。
- HTTP 403：认证失败，记录配置错误。

## 密钥规则

- 禁止提交真实 Bearer token。
- 禁止把 token 注入前端。
- 禁止在日志中打印完整 token。
