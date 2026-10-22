# API 文档

## 接口列表

基址 `/api/video-editing`。以下为 2026-10-22 当前实现的公开路由，浏览器全部使用已有飞书 Cookie 与 `video_editing` 模块权限；不需要旧剧集合成模块权限。写操作必须同源并发送 `Content-Type: application/json`，无参数写操作发送 `{}`。JSON 请求体不超过 2 MiB；视频字节不经过 CPU API。

| 方法 | 路径 | 输入 | 返回 / 用途 |
| --- | --- | --- | --- |
| GET | `/config` | 无 | `{item: Config}`，配置、接单开关与依赖可用性 |
| GET | `/assets` | `q`, `status`, `limit=50`, `offset=0` | `{items: Source[], total}`；limit 最多 100 |
| PATCH | `/assets/{asset_id}` | `{name}` | `{item: Source}`；仅上传者 / 管理员 |
| DELETE | `/assets/{asset_id}` | `{}` | `{item: Source}`；软删除，不物理删除冻结引用 |
| GET | `/sources/{source_id}` | 无 | `{item: Source}`；读取最新探测状态及临时预览链接 |
| GET | `/uploads` | 无 | 当前所有者尚未完成 / 取消的会话，最多 100 条 |
| POST | `/uploads` | `{operation_id,name,size_bytes,content_type}` | `{item: Upload}`；相同所有者与 operation_id 幂等 |
| GET | `/uploads/{upload_id}` | 无 | `{item: Upload}` |
| POST | `/uploads/{upload_id}/parts/{part_number}/sign` | `{content_md5}` | `{item: PartSignature}`；分片序号从 1 开始 |
| POST | `/uploads/{upload_id}/reconcile` | `{}` | `{item: Upload}`；查询 COS 原对象 / 原分片会话，不另建上传 |
| POST | `/uploads/{upload_id}/complete` | `{}` | `{item: Upload}`；服务端列出并核验分片后完成合并 |
| POST | `/uploads/{upload_id}/abort` | `{}` | `{item: Upload}`；取消原分片会话，已完成 / 合并中的上传不可取消 |
| GET | `/catalog/products` | 无 | `{items: Product[]}`；现有产品目录 |
| GET | `/catalog/dramas` | `app_id`, `q` | `{items: Drama[]}`；完整剧 ID 精确查找或剧名前缀，最多 50 条；不接受空查询 |
| GET | `/catalog/episodes` | `app_id`, `content_id` | `{items: Source[]}`；剧集首次读取可能为 `unprobed` |
| POST | `/catalog/probe` | `{source_ids: [...]}` | `{items: Source[]}`；每次 1–100 个不重复 ID；前端按 100 个分批 |
| GET | `/projects` | 无 | `{items: Project[], total}`；普通用户本人、管理员全部，最多 100 条 |
| POST | `/projects` | `Draft` 加可选 `operation_id` | `{item: Project}`；前端始终提供 operation_id |
| GET | `/projects/{project_id}` | 无 | `{item: Project}` |
| PATCH | `/projects/{project_id}` | `Draft` 加 `expected_version` | `{item: Project}`；版本匹配才保存，每次成功版本 +1 |
| POST | `/projects/{project_id}/render` | `{operation_id,expected_version}` | `{batch_id,project_version_id,items: Task[]}`；每部短剧一个任务 |
| GET | `/tasks` | 无 | `{items: Task[], total}`；制作任务列表，最多 100 条 |
| GET | `/tasks/{task_id}` | 无 | `{item: Task}`；也可查询有权限的探测任务 |
| POST | `/tasks/{task_id}/resume` | `{}` | `{item: Task}`；原 ID 核实 / 恢复，不新建成片任务 |

`asset_id` 为 `va_` + 32 位十六进制，剧集 source 为 `vep_`，upload 为 `vu_`，project 为 `vp_`，task 为 `ve_`，批次为 `vb_`，冻结项目版本为 `vv_`，成片为 `vo_`。普通用户读取他人的项目 / 任务返回 404；共享 `ready` 素材可读，管理权限由 `can_manage` 指示。

失败任务确认停止满 24 小时后可清理临时媒体。`resume` 仍使用原任务 ID：只有绑定配方指纹的清理记录、没有上传意图或未知旁证、且所有固定输出对象经鉴权 HEAD 明确返回 404，才允许以新执行代次重新下载制作；本地检查点使用新代次目录，旧审计记录保留。对象已存在、存储查询超时 / 403、旧式或损坏记录均不能作为重新制作的依据，返回 `gpu_job_resume_unavailable` 409 待查证。已验证成功成片始终直接复用。

`assets?status=all` 返回共享 ready 素材加本人的上传 / 校验状态，管理员可见全部；不传 status 仅返回 ready。项目和任务列表当前为最新 100 条，未实现分页输入；素材列表有分页。

## 请求/响应

### 配置与来源

```json
{
  "item": {
    "enabled": true,
    "intake_enabled": true,
    "max_file_bytes": 8589934592,
    "part_size": 16777216,
    "max_dramas": 20,
    "profile": "video-editing-720-v1",
    "worker_configured": true,
    "cos_configured": true
  }
}
```

`enabled` / `intake_enabled` 为同一新任务开关。关闭后仍可查询项目 / 成片、签已有上传的分片、完成已有上传与恢复旧任务；前端保留历史界面。`worker_configured` / `cos_configured` 仅表示服务配置存在，不代替实际联通验证。

`Source` 字段：`id, source_id, kind, name, status, owner_user_id, owner_name, can_manage, size_bytes, metadata, preview_url, thumbnail_url, expires_at, probe_task_id, error_code, error_message, created_at, updated_at`。剧集额外含 `app_id, content_id, episode_number, title`。`metadata` 探测后包含 `duration_ms,width,height,fps,has_audio`；前端不伪造未知时长。

`kind` 为 `asset` / `episode`；状态包括 `uploading,uploaded,unprobed,probing,ready,failed,deleted`。时间戳是 Unix 秒，片段时间为整数毫秒。临时媒体链接签发有效期 900 秒，重新 GET 来源 / 任务即刷新；前端将秒转毫秒显示有效期。已有公开剧集来源在无代理时可能直接提供原目录 URL。

### 产品与剧集目录

沿用现有产品目录的 `Product` 字段：`app_id,app,country,language,label`。前端优先显示 `label`，缺省使用 `app`，最终回退 `app_id`；展示名称不替换请求中的产品 ID。演示项目称为“批量剪辑”，避免将多剧分别出片误解为跨剧合成一片。

`q` 去除首尾空白后必须为 1–100 字符；1–32 位纯数字按完整 `content_id` 精确查询。其他输入至少 2 字符；符合 `[A-Za-z0-9_-]` 的标识先尝试 `content_id` 精确查询，未命中且非纯数字时再按剧名开头匹配。中文等剧名直接按前缀查询，`%` / `_` 作为普通字符转义，不执行包含式模糊检索。前后端都拒绝空查询或非数字单字符查询。

目录查询使用现有 `content_id` / `name` 索引，每次 SQL 最多读取 500 行，服务端按剧 ID 去重后最多返回 50 部。单 SQL 执行预算 5 秒，MySQL 连接超时 3 秒，包含既有 SQL 排队门控的进程预算 15 秒；超时结束该查询的进程组并返回 `catalog_query_timeout` 503。预算按单次查询计算，不承诺整个 HTTP 请求总耗时小于 15 秒。产品枚举、剧集读取也复用有界查询包装，不改变其他模块共享的查询函数。

`Drama` 字段为 `app_id,content_id,title,episode_count,cover_url`。剧集 `source_id` 由产品、剧 ID、集号和目录 URL 派生；目录 URL 变化创建新的来源身份。读取旧 episode source、保存及新制作时重新核对当前产品目录可用性；已冻结任务仍通过原任务读取 / 恢复。

### 上传

`Upload` 字段：`id,asset_id,status,name,size_bytes,part_size,part_count,uploaded_parts,created_at,expires_at,error_code`。`uploaded_parts` 为 `{part_number,etag,size_bytes}` 数组。

`PartSignature` 字段：`url,headers,expires_at,part_number,size_bytes`。浏览器以 PUT 上传该分片 Blob，逐项采用服务端返回 headers，包含 `Authorization,Content-MD5,Content-Type,x-cos-psize-min,x-cos-psize-max`；禁带站点 Cookie。`content_md5` 是该分片 RFC 1321 MD5 的标准 base64。同一分片再次签名不能更换内容。前端每文件最多并发 3 个分片。

允许 MIME：`video/mp4,video/quicktime,video/webm,video/x-matroska,video/x-msvideo,video/mpeg,application/octet-stream`。大小 1 字节–8 GiB，文件名 1–240 字符；上传会话期限 7 天。浏览器 localStorage 仅保存原文件标识、内部会话 ID 和 operation_id，不保存签名 URL 或鉴权 headers。

会话状态：`initializing,initializing_unknown,uploading,completing,completing_unknown,completed,aborting,abort_unknown,aborted`。初始化 / 合并 / 取消网络结果未知时必须 reconcile 原会话。服务端保存完成 / 取消开始时间；若进程在落账后崩溃，120 秒操作预算届满后 reconcile 将悬停状态转为对应 unknown，再依据原 UploadId、冻结分片清单及 COS 回读继续核实，不盲目新建上传。完成校验包括分片序号 / 数量 / 大小 / ETag 与 MD5、最终对象 nonce / 大小及私有 ACL；成功才进入媒体探测。

### 草稿与版本

```json
{
  "name": "悬念开场批量剪辑",
  "asset_id": "va_00000000000000000000000000000001",
  "dramas": [{
    "app_id": "product-id",
    "content_id": "drama-id",
    "title": "短剧标题",
    "clips": [
      {"id":"clip_a","source_id":"va_00000000000000000000000000000001","start_ms":0,"end_ms":5000,"fit":"contain","mute":false,"transition":"fade"},
      {"id":"clip_b","source_id":"vep_00000000000000000000000000000002","start_ms":12000,"end_ms":26000,"fit":"cover","mute":false,"transition":"cut"}
    ]
  }]
}
```

`Project` 为上述 Draft 加 `id,version,created_at,updated_at`。名称 1–120 字符，每项目最多 20 部不重复短剧，每剧最多 300 个片段，片段 ID 在单剧内唯一。未完成选择的空草稿允许保存。

`transition` 属于当前片段到下一个片段，取 `cut|fade`，末段必须 cut。fade 固定 0.3 秒 / 9 帧，声音同步淡化。`fit` 为 `contain|cover`；mute 为布尔。片段 source 必须属于选中的唯一素材或当前短剧；同一素材允许重复插入。

制作前至少包含一个素材片段和一个剧集片段；所有来源 ready。30fps 端点按 `floor((ms*30+500)/1000)` 转为帧，保留范围至少一帧，片段总帧数不得少于两侧淡化所需帧数。最终时长是各保留帧数之和减去所有淡化重叠帧数。前端使用相同算法提示，服务端再次验证。

POST project 相同 operation_id + 同内容返回已建项目，内容不同返回 409。PATCH 需 expected_version；前端保留编辑中更新，换项目前刷完保存。PATCH 响应未知时先 GET 核对已存版本，不能直接重发改变版本。制作请求 operation_id 在未编辑时保持稳定；新编辑改变项目版本，已提交任务冻结配方不受后续编辑影响。

### 制作批次、冻结版本与成片账本

```json
{
  "batch_id": "vb_00000000000000000000000000000001",
  "project_version_id": "vv_00000000000000000000000000000002",
  "items": [{
    "id": "ve_00000000000000000000000000000003",
    "project_id": "vp_00000000000000000000000000000004",
    "batch_id": "vb_00000000000000000000000000000001",
    "project_version_id": "vv_00000000000000000000000000000002",
    "output_id": "vo_00000000000000000000000000000003",
    "kind": "render",
    "status": "queued",
    "output": null
  }]
}
```

上例省略普通任务展示字段。提交在同一 SQLite 事务中保存版本快照、批次、逐剧任务和待完成成片记录；出片成功并通过 COS 校验后才将成片记录改为 ready。

| 持久表 | 唯一约束 / 内容 | 生命周期 |
| --- | --- | --- |
| `ve_project` | 项目 ID；当前草稿及整数 version | 编辑成功递增版本 |
| `ve_project_operation` | `(owner,operation_id)`；项目 ID、首次创建草稿 hash | 核实未知创建结果 |
| `ve_render_operation` | `(owner,operation_id)`；实际请求人、project_id、project_version、batch_id | 多个客户端操作可指向同一项目版本批次 |
| `ve_project_version` | ID 与 `(project_id,version)` 唯一；冻结 Draft、所有者和时间戳 | 同版本只保存一个快照；不受后续编辑影响 |
| `ve_batch` | ID 与 `(owner,operation_id)` 唯一；project_id、project_version、project_version_id、task_ids | 同一提交返回原批次及原逐剧任务 |
| `ve_task` | ID 与 `(owner,operation_id)` 唯一；冻结 GPU 配方、身份及状态 | 每部剧独立完成 / 原任务恢复 |
| `ve_output` | 稳定 ID 与 task_id 唯一；owner、batch_id、status、结果及对象校验数据 | 提交时 pending，COS 校验成功后 ready |

同一制作 operation_id 重放必须携带原 project_id 和 expected_version；后端先查持久操作映射，再兼容历史批次的原操作号。新的操作号也会在同一写事务内按 project_id + expected_version 复用最早接受的批次；刷新、多标签及重新打开项目均不会新建同版本任务。原批次无论排队、失败或成功均返回原任务状态，失败只通过任务 resume 显式恢复。项目编辑产生新版本后才允许新批次。后续草稿编辑、素材软删除或关闭新建入口不妨碍授权用户找回原批次；原操作参数变化返回 `operation_conflict`。

新批次、任务与成片归项目所有者，实际提交人在 batch / task 的 submitted_by 中记录。请求操作映射按提交人隔离，内部批次操作号使用提交人及外部操作号摘要，避免管理员代提交发生冲突。历史批次、任务和归属不迁移；若历史批次中的任务不满足当前 task 读取权限，返回 `batch_task_permission_conflict` 409，要求管理员查看，不能借复用批次扩大权限。历史已存在重复批次保留；原操作号仍返回原批次，新操作选择最早的固定批次。

成片稳定 ID 与 COS 临时预览 URL 分离；对象身份、校验值、规格、所有者和来源配方保存在内部账本。当前没有独立的批次 / 成片列表路由，读取入口仍为 render 返回值及 Task DTO；外部制作适配器后续可引用 output_id。

### 任务与内部 GPU 接口

`Task` 字段：`id,project_id,batch_id,project_version_id,output_id,kind,status,drama_title,stage,progress,error_code,error_message,output,created_at,updated_at`。probe 任务的 batch / version / output ID 为空字符串。制作状态包括 `queued,submitting,queued_gpu,running,unknown,recovery_required,failed,resume_queued,completed,cancelled`。`progress` 可为数值或 GPU 阶段信息，前端至少显示状态 / 阶段，数值可用时显示百分比。

完成的 `output` 含 `id`（等于 output_id）、临时 `preview_url,download_url,thumbnail_url,expires_at`，以及元信息 / 校验值；`metadata` 包含成片的 duration_ms,width,height,fps,has_audio。内部 bucket / key 不直接暴露到该 DTO。CPU 只在验证 GPU 任务身份、冻结配方指纹、COS 大小 / SHA256 / 私有 ACL 后接受成片。恢复仅接受 `failed,unknown,recovery_required`，其余状态返回 409；旧轮询响应不得覆盖用户已提交的 resume_queued，完成回执优先保留。

内部 GPU 接口由已有 Bearer 验证，不向浏览器开放：

| 方法 | GPU 路径 | 契约 |
| --- | --- | --- |
| POST | `/api/gpu-video/video-editing/jobs` | `{job_id,task_type:"video_editing_v1",operation:"probe"或"render",recipe}`；成功 202 |
| GET | `/api/gpu-video/video-editing/jobs/{job_id}` | 返回原 runtime 状态 / generation / 结果 |
| POST | `/api/gpu-video/video-editing/jobs/{job_id}/resume` | 同一冻结 payload 加 `expected_generation`；成功 202 |

内部 recipe 含 `version:1,profile,sources,clips`；sources 由 CPU 从可信目录 / COS 记录冻结 URL、身份、媒体元信息，浏览器不得自行指定 URL。旧 GPU route 拒绝新 `ve_` 命名空间；新任务通过同一调度器与现有单渲染槽位运行。

## 错误码

统一 `{code,error}`，HTTP 状态保留业务语义；未知异常返回 503 `video_editing_unavailable`，不回传凭证或内部异常堆栈。

| 分组 | 常见 code / HTTP | 客户端动作 |
| --- | --- | --- |
| 身份权限 | 既有 Cookie / 模块权限 401/403；`permission_denied` 403；对象 `*_not_found` 404 | 登录、管理员授权或停止越权操作 |
| 接单 / 配置 | `video_editing_intake_disabled` 503；COS / GPU 配置不可用 | 保留草稿与旧任务；不降级到服务器本地媒体存储 |
| 请求 | `request_invalid,json_object_required,part_md5_invalid,clip_range_invalid,clip_style_invalid` 400；`request_too_large` 413 | 定位字段，保留输入 |
| 目录 | `product_required,query_required` 400；`catalog_query_timeout,catalog_query_failed` 503 | 选择产品，输入完整 ID 或更长剧名前缀；不自动改成空查询 |
| 内容归属 | `product_not_available` 403；`clip_source_mismatch,source_not_ready` 409 | 选择有效来源 / 等待校验 |
| 版本与幂等 | `project_version_conflict,operation_conflict` 409 | 保留本地编辑，读回核对；不可覆盖其他版本 |
| 上传 | `upload_not_writable,part_content_changed,parts_incomplete,part_identity_mismatch` 409 | 保留同一会话，重新选择原文件并核实 |
| COS 未知结果 | `cos_initialize_unknown,cos_complete_unknown,cos_abort_unknown,cos_reconcile_failed` | reconcile 原会话，禁止盲目初始化新对象 |
| 剪辑参数 | `video_editing_clip_too_short,video_editing_last_transition,video_editing_transition_too_long` | 明确片段位置并要求修改，不静默改变效果 |
| 恢复 | `task_not_resumable,probe_superseded` 409；GPU generation 冲突 | 重新 GET 原任务，按最新状态核实 |

CPU 兼容 GPU runtime 的 `error.code` 及旧顶层 `error_code`，保留受限字符的错误码并映射为固定安全中文。例如 `video_editing_disk_budget` 提示空间不足，`video_editing_transition_too_long` 提示转场片段过短；不把远端原始异常或 URL 直接展示给浏览器。

## 兼容性说明

- 新 SQLite 业务数据 / 路由 / 权限独立，旧项目 API、配方指纹、缓存键与结果 DTO 不变。
- 同一素材 soft-delete 不删除已冻结任务的 COS 对象；撤销入口或回滚页面仍须保留识别新任务的 GPU 调度器。
- 外部制作 / 盈量入库本期没有调用路由，页面仅显示待接入，不会自动上传给外部后台。
- `?demo=1` 使用明确标注的内存模拟数据，代码阻止对 `/api/video-editing` 发出业务请求；正式模式 API 失败不会回退假数据。
