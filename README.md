# Drama Material Service

提供两个接口：

- `POST /api/drama-material/jobs`
  - 入参：`app_id`、`content_id`、`episode_start`、`episode_end`
  - 功能：先按 `app_id + content_id` 校验剧集和范围是否可用，通过后才创建任务、返回可用性信息并落库；随后下载封面和选定剧集，生成 16:9 封面，制作首帧 intro，拼接视频，并把结果落到本地 SQLite 任务表
- `GET /api/drama-material/jobs`
  - 查询参数：`job_id`，或 `app_id + content_id`，可选 `limit`
  - 功能：拉取任务和产物信息

## Run

```bash
cd /root/drama_material_service
python3 app.py
```

默认监听 `0.0.0.0:8787`。

## Environment

可选环境变量：

- `DRAMA_API_PORT`
- `DRAMA_WORK_ROOT`
- `DRAMA_PUBLIC_ROOT`
- `DRAMA_PUBLIC_BASE_URL`
- `DRAMA_FFMPEG`
- `DRAMA_FFPROBE`
- `OPENAI_API_KEY`
- `OPENAI_IMAGE_MODEL`
- `OPENAI_IMAGE_SIZE`
- `OPENAI_IMAGE_QUALITY`
- `DRAMA_JOB_DB_PATH`

说明：

- 当前环境里的远端 MySQL 是只读库，因此结果任务表默认落本地 SQLite：`/root/drama_material_service/data/drama_material_jobs.sqlite3`
- 当前环境里的默认产物目录是 `/usr/share/nginx/html/drama-materials`，默认公网前缀是 `https://ai.yingliangads.com/drama-materials`
- `schema.sql` 仍然保留了 MySQL 建表语句，方便你后续切到可写库时直接复用
- 如果设置了 `OPENAI_API_KEY`，服务会调用 OpenAI `images/edits` 做真正的横版 outpainting：先把竖版原图放到横版透明画布中间，再由模型补全两侧内容。
- 如果没有设置 `OPENAI_API_KEY` 或 AI 生图失败，任务会直接失败，不再退化为模糊背景或剪辑式扩展。
- `drama-material-api.service` 已支持读取 `/root/drama_material_service/.env`，可以把 `OPENAI_API_KEY=...` 放进去后重启服务。
- 可以直接参考 `/root/drama_material_service/.env.example` 复制出 `.env`。

## Submit Example

```bash
curl -sS -X POST 'http://127.0.0.1:8787/api/drama-material/jobs' \
  -H 'Content-Type: application/json' \
  -d '{
    "app_id": "1479",
    "content_id": "fQ6YzE68r9",
    "episode_start": 1,
    "episode_end": 11
  }'
```

成功响应会额外返回：

- `content_available`
- `app_id`
- `drama_name`
- `app`
- `country`
- `language`
- `total_episodes`
- `available_episode_start`
- `available_episode_end`

如果 `content_id` 无效或集数越界，接口会直接返回 `400`，不会创建失败任务记录。

## Query Example

```bash
curl -sS 'http://127.0.0.1:8787/api/drama-material/jobs?app_id=1479&content_id=fQ6YzE68r9&limit=10'
curl -sS 'http://127.0.0.1:8787/api/drama-material/jobs?job_id=<job_id>'
```
