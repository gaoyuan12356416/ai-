# 018. 素材任务状态飞书播报接口需求与技术设计

## 背景

甲方系统会在素材任务形成最终状态后，将任务数据推送给我方。服务需要依据甲方传入的优化师名称定位内部用户，并实时向对应飞书用户私聊播报；无法完成私聊时，必须在固定兜底群中说明失败原因，避免消息丢失。

## 目标

- 提供一个稳定、可重试、可审计的 HTTPS JSON 接口。
- 使用独立 Bearer Token 鉴权，不限制来源 IP。
- 严格按 `admin_users.username -> admin_users.id -> admin_user_group.sub_user_id -> admin_user_group.email` 匹配。
- 通过飞书 `batch_get_id?user_id_type=open_id` 将 email 解析成 open_id 后私聊。
- 优化师、邮箱或飞书用户无法匹配，以及私聊最终失败时，发送到群 `oc_88f2eb329508d13bfd2be3de0e221797`。
- 用幂等键和持久化 outbox 避免甲方重试造成重复播报，并保证服务重启后可继续投递。

## 范围

### 包含

- `POST /api/integrations/v1/material-task-status-events`
- 独立 Bearer Token、Token 轮换和恒定时间比较
- 32 KiB 请求体上限、JSON/字段/RFC 3339 时间校验
- SQLite 入站事件与 outbox 状态
- 优化师、邮箱和飞书 open_id 匹配
- 飞书私聊和兜底群播报
- 失败重试、dead letter、审计字段
- 对外接口文档、部署文档、测试用例和公开可分享的 HTML 文档

### 不包含

- 根据别名、中文名、模糊关键词猜测优化师
- 修改 `admin_users`、`admin_user_group` 或飞书通讯录数据
- IP 白名单
- 甲方可控制的 `dry_run`
- 后台人工重放页面

## 业务规则

1. 请求必须携带 `Authorization: Bearer <token>` 和 `Idempotency-Key`。
2. 请求体严格包含十个字段：
   `resource_id`、`resource_name`、`task_start_time`、`drama_dubbing_type`、
   `task_type`、`original_material_name`、`material_name`、`language`、
   `final_status`、`optimizer_name`。
3. `optimizer_name` 字段必须存在，允许空字符串；空字符串直接视为无法匹配并进入兜底群。其余九个字段必须为非空单行字符串。
4. `task_start_time` 必须是带时区的 RFC 3339 时间，秒的小数部分支持 1–6 位；服务统一以 UTC 保存，播报时转换为 UTC+8。
5. `optimizer_name` 只去除首尾空白后与 `admin_users.username` 做大小写敏感的精确匹配，不做模糊匹配。
6. 只使用 `admin_user_group.status=0` 且 email 非空的映射。
7. email 必须通过飞书接口解析成 open_id，私聊时使用 `receive_id_type=open_id`。
8. 下列情况进入兜底群：
   - 优化师名称为空或 `admin_users` 未命中；
   - 有效 email 缺失或冲突；
   - email 无法解析成飞书 open_id；
   - 私聊永久失败，或临时失败达到最大重试次数。
9. 同一幂等键和相同请求体只创建一个事件；同一幂等键对应不同请求体返回冲突。
10. 接口成功接收并可靠落库后返回 `202`；同键同内容重放也统一返回 `202` 并携带当前 `delivery_status`，不把“已接收”表述成“飞书已送达”。

## 流程

```text
Bearer Token + JSON/字段校验
              |
              v
     幂等事件与 outbox 落库
              |
              v
           返回 202
              |
              v
admin_users.username -> admin_users.id
              |
              v
admin_user_group.sub_user_id -> email
              |
              v
      Feishu email -> open_id
          /                 \
       成功                  失败
        |                     |
      私聊              兜底群说明原因
```

## 技术设计

### 影响模块

- `app.py`：配置、Token 鉴权、路由、MySQL/飞书适配器、后台 worker
- `features/material_status_broadcast/`：字段规范化、消息格式、幂等 outbox
- `.env.example`：非敏感配置占位
- `deploy/nginx/material-status-webhook.conf`：精确路由反向代理
- `scripts/test_material_status_broadcast.py`：纯模块测试
- `scripts/test_material_status_webhook_app.py`：接口编排测试

### 数据结构

SQLite 事件表至少保存：

- `event_id`、`idempotency_key`、请求体 SHA-256
- 规范化请求体、资源 ID、优化师名称、来源 IP
- `status`、`delivery_kind`、`attempt_count`、下次重试时间
- 失败码和脱敏失败说明
- `admin_user_id`、脱敏 email、飞书 message_id
- 创建、更新和送达时间

不保存 Bearer Token、飞书 App Secret、tenant token、完整 open_id。

### 线上现状核对

- 生产服务使用只读 MySQL 端点，现场确认 `@@read_only=1`。
- `admin_users.username` 有唯一索引。
- `admin_user_group.sub_user_id` 与 `admin_users.id` 是现有生产关联方式，活动记录使用 `status=0`。
- 当前 262 个有效非空 email 映射中，209 个可解析到飞书 ID；其余应按本需求进入兜底群。
- 飞书应用 tenant token、email 批量解析和消息发送权限已核对。
- 兜底群成员接口返回成功，说明机器人可访问该群。

### 异常与边界

- Token 缺失/错误：`401`
- 非 JSON：`415`
- 请求体过大：`413`
- 字段或时间错误：`422`
- 幂等键冲突：`409`
- 事件库不可用或服务未配置：`503`
- 飞书/数据库临时错误：后台指数退避，不要求甲方重复创建事件
- 兜底群也无法发送：进入 `dead_letter`，不得标记成功

## 验收标准

- 合法新请求在可靠落库后返回 `202`，P95 接收时间不超过 1 秒。
- 正常依赖下，私聊或兜底群播报在 5 秒内出现。
- Token 不出现在代码、Git、响应、日志、文档或审计数据中。
- 同一幂等键相同请求串行和并发重放均不重复创建正常播报。
- 十个字段按固定顺序完整展示；私聊与兜底业务字段顺序完全一致。
- 所有无法私聊的业务原因均能在兜底群看到事件编号和失败码。
- 服务重启后未完成事件可继续处理。

## 风险与待确认

- 实际私聊生产 canary 需要指定一个不会打扰业务的优化师账号；未指定时只做查询验证、自动化测试和兜底群受控 canary。
- 飞书“发送成功但本地因网络中断未确认”无法实现绝对 exactly-once；消息中的事件编号用于识别极端重复。
- 私聊和兜底分别使用稳定的飞书消息 UUID；飞书在一小时内对相同 UUID 去重，覆盖常规网络超时和短时重启重试。

## 变更记录

- 2026-07-28：首次建立需求，鉴权方案按用户最新指示确定为独立 Bearer Token，不使用 IP 白名单。
- 2026-07-28：请求契约由八字段升级为十字段，新增 `resource_name`（资源名）和
  `drama_dubbing_type`（剧集配音类型），保留既有 `task_type`（任务类型）；
  三项均进入私聊和兜底播报。旧八字段请求按严格契约返回 `422`。
