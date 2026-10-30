# 素材复刻 API

路径前缀：`/api/video-editing`。沿用登录 cookie、video_editing 模块权限、同源 JSON 写入检查及任务 owner/管理员授权。固定 B 组 token 只由独立 worker 使用。

## GET /tasks/{task_id}/material-replication

返回 `{"item": context}`。

| 字段 | 含义 |
|---|---|
| enabled / disabled_reason | 本条成片当前是否允许提交及原因 |
| source | material_id、name、resource_id、source_language、duration_ms；服务端从成片与成功入库账本确定 |
| default_operator / operator_error | 创建人盈量账号及无法唯一解析时的说明 |
| can_change_operator | 仅管理员为 true |
| target_languages | 本剧资源可选语言 code/name，包含母素材源语种 |
| history / summary | 持久提交回执及摘要；不代表成品或回推进度 |

`?history_only=1` 用于核实或刷新记录，不查询外部资源/人员目录。功能关闭时仍可读取既有历史；关闭不等于删除回执。

普通上下文将原任务 owner 的内部登录邮箱映射到有效盈量 username。已明确核实的历史归属纠正存在新 metadata 表，不改原任务 owner、submitted_by 或原入库账本。

## POST /tasks/{task_id}/material-replication

```json
{
  "operation_id": "client-operation-unique-0001",
  "task_type": 1,
  "audio_type": 0,
  "target_material_type": 1,
  "target_langs": ["es"],
  "no_bgm": false,
  "operator": "susiqi"
}
```

- operation_id：8–128 位字母、数字、下划线或连字符；一次逻辑请求冻结并保留，丢响应时沿用原值。
- task_type：1/3/4；audio_type：0/1/2；target_material_type：1/2，均要求整数。
- target_langs：1–30 个目标代码，服务端规范化为唯一集合；必须来自本剧资源选项。默认不选，可选源语种。
- operator：管理员可以填写；普通成员省略或只能使用服务端解析出的默认归属。
- no_bgm：布尔值，表示母素材没有背景音乐。
- 解说时额外传 descant_start_time、descant_end_time，格式 HH:MM:SS 或 HH:MM:SS.SSS；0 ≤ 开始 < 结束 ≤ 成片时长。
- 母素材 material_id、resource_id、URL、名称、时长和源语种由服务端提供，不允许客户端传入覆盖。

成功返回 `{"item": record}`。新请求先写入 pending，再由 worker 发送；HTTP 200 不表示 B 组已受理。record 包含 id、operation_id/operation_ids、status、冻结制作参数、申请/有效/unsupported 语种、upstream_status、b_task_id、b_task_material_ids、error、时间和历史标识。

状态：pending（本地待发送）、submitting（已登记出站）、accepted、partial、rejected、unknown_outcome。accepted/partial 的 upstream_status 可能为 queued、waiting_download、duplicate，均仅是受理时快照。

## 幂等和冲突

同一操作者 operation_id 重放相同参数返回已登记记录；不同参数返回 409，不能悄悄换 ID 重发。服务端还按 B 组精确键（母素材 URL、audio_type、task_type、规范化目标语种集合）复用 pending/submitting/accepted/partial/unknown_outcome。若归属、解说或其他不在 B 键内的参数不同，返回旧记录及 `reused/conflict/conflict_fields`，界面必须指出本次新参数未生效。

不同目标语种集合不会自动做差集；有重叠的集合可能重复制作公共语种。明确拒绝允许用户修正后新建操作；未知结果不得自动重新出站。

## B 组出站边界

唯一出站地址为 `https://marketing.mydramawave.com/api/internal/material/a-group-task-create`。鉴权使用自定义 HTTP 请求头 `token: <private value>`，不是 Authorization/Bearer。

配置读取为 worker 的 A_GROUP_TASK_CREATE_BASE_URL 和 A_GROUP_TASK_CREATE_TOKEN；API 和浏览器不读取 token。不跟随重定向，不自动重试 POST，不将原始请求、token 或原始 HTTP 错误写入日志。HTTP 非 200、坏响应、超时及发送中崩溃按未知处理；HTTP 200 内仍需核对业务码和回执一致性。

## 列表与错误

Task DTO 增加 material_replication 的 enabled、disabled_reason、summary、count。3 秒列表轮询只读本地 SQLite；摘要优先最新非历史记录，只有历史记录时优先有效受理。

常见拒绝：401 未登录，403 非管理员改归属，404 无任务访问权，409 成片/素材/来源未就绪或操作标识冲突，503 功能关闭或依赖不可用。前端须保留未知请求，不以失败 toast 掩盖可能已受理的操作。
