# 剧集合成异步制作API

更新：2026-08-28。对应当前 `async_runtime`、GPU worker及CPU客户端实现；接口未在生产切换。示例均为说明数据，不是可直接执行的正式任务。

## 边界、认证与接口列表

GPU服务仅监听 `127.0.0.1:8787`；CPU使用现有隧道 `http://127.0.0.1:18788`。新接口必须有 `Authorization: Bearer <worker-token>`。Token只存目标机器受限配置，不能放进文档、URL或浏览器。响应为UTF-8 JSON、`Cache-Control: no-store`；下列任务响应是裸对象，不包在 `item` 内。

| 方法/路径 | 行为 | 正常响应 |
| --- | --- | --- |
| POST `/api/gpu-video/jobs` | 持久提交或重连同一任务 | 202＋任务DTO，含已完成/失败的已有记录 |
| GET `/api/gpu-video/jobs/{job_id}` | 查询已接受记录，不抢制作名额 | 200＋任务DTO |
| POST `/api/gpu-video/jobs/{job_id}/resume` | 用户明确请求的受控恢复 | 202＋当前/下一代次DTO |
| POST `/api/gpu-video/render` | 保留同步调用；共用身份、账本和名额 | 200＋原制作结果；运行中不另起制作 |
| POST `/api/gpu-video/cover` | 独立封面回调 | 200＋`{"job_id":"...","ok":true}` |
| GET `/api/gpu-video/random-overlay/catalog` | 已有认证素材目录 | 保持已有合同 |
| GET `/healthz` | 媒体worker存活检查，无业务/发布能力 | 保持已有健康响应；不能替代渲染验收 |

## 提交请求与身份

请求体最多2 MiB。`job_id` 满足 `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`；新异步任务必须有1～1000条唯一正整数集号的episodes、有效HTTP(S)素材URL及至少一个视频输出。输出和等待封面标志为布尔值。内容ID不能含路径分隔或控制字符，UTF-8长度最多200字节。

```json
{
  "job_id": "doc-example-only",
  "content_id": "example-drama",
  "episode_start": 1,
  "episode_end": 2,
  "outputs": {
    "concat_video": true,
    "no_bgm_video": false,
    "random_template_video": false
  },
  "cover_16x9_url": "",
  "await_cover_16x9": true,
  "episodes": [
    {"episode_number": 1, "episode_url": "https://source.example.test/1.mp4"},
    {"episode_number": 2, "episode_url": "https://source.example.test/2.mp4"}
  ]
}
```

选择随机模板时另传已冻结的 `random_template_recipe`，包含可重新计算的 `recipe_sha256`；源类型只能为 `concat_video` 或 `no_bgm_video`，实际制作继续校验既有profile、素材和配方合同。

新冻结任务可为每集增加 `download_route`，严格包含 `version: 1`（整数）、`source_url`、`primary_url`、`fallback_url`（字符串）四个字段。`source_url` 必须等于该集 `episode_url`；默认原源策略使用 `primary_url=source_url`、`fallback_url=""`。经门禁启用的国际线路只对无签名参数、无端口且符合白名单路径的 `img.tianmai.cn/resource/.../*.mp4` 派生 `accelerate.tianmai.cn` 主源，并固定原源作回退。运行时检查结构和HTTP(S) URL，下载器再校验精确派生关系；不允许任意代理地址。未知字段、错误版本、非对象线路或不一致源地址直接拒绝。旧payload无该字段时不自动补入，保持既有身份。

指纹由共享 `render_fingerprint(payload)` 生成，覆盖job、内容ID、集范围、输入列表顺序与源URL、显式下载线路全部字段、输出开关、是否有封面片头及随机配方。提交时已经固定的封面URL也纳入身份；只有保留原 `await_cover_16x9=true`（或 `wait_for_cover=true`）的等待封面任务，晚到URL不改变身份，封面回调另以首次绑定保护，不能覆盖不同URL。重连不能把等待标志擅自改为false。等待时长和恢复请求代次不是媒体身份；“无片头”与“有片头”仍不同。CPU须保存并重用完整原payload，不能在重连时重新查源、改域名或改输出选项。

## 任务DTO

```json
{
  "job_id": "doc-example-only",
  "fingerprint": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "generation": 1,
  "status": "running",
  "stage": "downloading",
  "progress": {
    "completed_episodes": 1,
    "total_episodes": 2,
    "downloaded_bytes": 1000000,
    "total_bytes": 2000000
  },
  "created_at": "2026-08-28T08:00:00.000+00:00",
  "started_at": "2026-08-28T08:00:01.000+00:00",
  "heartbeat_at": "2026-08-28T08:00:10.000+00:00",
  "last_progress_at": "2026-08-28T08:00:10.000+00:00",
  "completed_at": null
}
```

| 字段 | 合同 |
| --- | --- |
| `fingerprint` | 冻结输入的64位SHA256；客户端必须核对 |
| `generation` | 从1开始的正整数；返回低于已知代次应停止回填 |
| `status` | `queued`、`running`、`completed`、`failed`、`recovery_required` |
| `stage` | 执行阶段，可能为queued/starting/downloading/normalizing/waiting_cover/rendering_intro/concatenating/removing_bgm/rendering/rendering_random/uploading/verifying/completed/failed/recovery_required |
| `progress` | 只含已知、有限、非负数值指标；不能包含URL、PID、命令或自由文本 |
| 时间字段 | UTC ISO8601；未开始/未完成可为null；首次开始不因恢复清零 |
| `result` | 仅completed存在；包含已发布媒体URL及随机模板的输出/配方SHA、profile等既有公开结果字段 |
| `error` | 失败/待核查时为`{"code":"固定错误码","message":"固定中文"}`；无内部异常文本 |

常用指标：下载为 `completed_episodes,total_episodes,downloaded_bytes,total_bytes,bytes_per_second`；标准化为 `normalized_episodes,total_segments`；模板为 `out_time_seconds,duration_seconds,frame,fps,speed`；上传为 `uploaded_bytes,total_bytes`。字段可暂缺；客户端不能补造总进度或速度。心跳更新不等同于实际进展。

## 查询与恢复规则

1. CPU用连接/读取超时 `(3,15)` 秒，每10秒查询；没有整个制作的四小时轮询截止。客户端禁止跟随HTTP重定向。
2. 没有任何接受证据且GET返回权威 `404 gpu_job_not_found` 才可同payload提交。丢响应先查询；已知记录后来404必须停止重提并核查。
3. 相同job/指纹的普通提交总是复用原记录，failed也不会隐式重新执行。完成缓存校验在重制作名额之外进行；错误不能被转换成缓存未命中。
4. resume体使用原payload，另加 `"expected_generation": 1`，且路径job_id必须相同。必须确认原执行已停止、无未知启动窗口并允许进入阶段检查后，才能由1变2。
5. 当前已经是期望代次＋1，重复resume只返回该记录，即使该代次刚失败也不能再启动。其他过期代次冲突；queued/running/completed在当前代次下不启动新执行。
6. 进程存活或未知、账本/成片无法校验时停止自动重制。工作目录存在仅表示允许进入恢复校验，不能证明本地成片可复用；各阶段仍独立验证检查点。

## HTTP错误与任务失败的区别

HTTP错误采用 `{"code":"...","error":"安全中文"}`。GET返回HTTP200但 `status=failed/recovery_required` 是已读取到明确执行结果，不能当网络错误无限重试。

| HTTP/错误码 | 意义与处理 |
| --- | --- |
| 400 `invalid_request` / `invalid_job_id` / `invalid_content_id` | 请求无效，修正后再提交；不开始制作 |
| 401 `unauthorized` | 内部Token无效；不能向用户暴露Token |
| 404 `gpu_job_not_found` | 当前worker未找到账本；只有未曾确认的任务允许幂等重提 |
| 409 `gpu_job_input_conflict` | 同job冻结输入不同，停止重制 |
| 409 `gpu_generation_conflict` | 恢复代次过期，先查询，不盲加代次 |
| 409 `gpu_job_resume_unavailable` / `gpu_process_state_unknown` | 无法安全恢复，人工核查 |
| 503 `gpu_queue_full` / `gpu_render_busy` | 容量不足，保留同一任务并等待；不是GPU利用率满载证明 |
| 503 `gpu_job_running` | 旧同步调用遇到已有执行，应查询该记录 |
| 503 `gpu_runtime_unavailable` | 节点尚未就绪或停止接新；不作为权威404 |
| 503 `gpu_runtime_unverified` / `gpu_result_cache_unverified` | 状态/成片无法校验，明确停止自动重制，不按普通网络失败重提 |
| DTO `gpu_previous_process_running` / `gpu_process_state_unknown` | 旧执行仍运行或无法确认，保留产物和账本核查 |
| DTO媒体错误 | 例如源版本变化、下载不完整、检查点冲突、模板规格/时长不符；只展示固定本地中文映射 |

## CPU业务API兼容与私有存储

既有 `/api/drama-material/jobs` 和任务详情/重试权限保持不变。任务响应按需增加 `remote_runtime`、`remote_progress` 和首次活动时间；`remote_progress` 包含阶段中文、阶段百分比及说明。旧记录没有这些字段时保持原有展示。

既有用户重试在异步模式下持久化一次期望代次恢复意图，再由CPU调用resume。完成结果在CPU通过租约栅栏与配方事务对账，通知失败不得回到媒体重试。`DRAMA_GPU_ASYNC_ENABLED=0` 时不切换CPU调用协议；已有新账本的未完成任务不能直接交给不理解异步状态的旧代码处理。

GPU私有账本和CPU冻结payload包含源URL，必须受限存储和备份；状态DTO不返回这些字段。新完成manifest版本为v3，`input_fingerprint` 必须是当前冻结payload的64位十六进制指纹并精确匹配。每个选中产物必须带 `bucket`、`key`、`sha256`、`size_bytes`、`etag`、`binding` 和返回URL；读取时重新计算仍保留的本地文件并用认证HEAD核对远端对象。任何缺失、变化或不一致均返回恢复类错误，不把记录当成缓存未命中，也不从文件名推导URL。旧未版本化manifest只供旧同步兼容读取，不能据此完成新异步执行或删除新账本。
