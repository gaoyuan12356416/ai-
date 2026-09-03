# 内部 API

均位于 GPU loopback 服务并要求 Bearer：`GET /api/gpu-video/youtube-media/health`；`POST .../prepare` 接收 `task_id,source_url`；`POST .../upload` 接收 `task_id,session_uri,offset,size,sha256`；`POST .../cleanup` 只清对应 task。响应 no-store，URL 和会话不得记录。
