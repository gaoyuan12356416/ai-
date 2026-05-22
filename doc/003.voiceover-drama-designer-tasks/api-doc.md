# API 文档

## GET /api/voiceover-drama/designers

返回 `[78]授权设计师接单权限用-设计师接单权限` 下可选设计师。

响应：

```json
{
  "items": [
    {
      "user_id": "248",
      "sub_user_id": "248",
      "name": "郜远",
      "email": "gaoyuan@yingliangads.com",
      "username": "gaoyuan",
      "label": "郜远 / gaoyuan"
    }
  ],
  "group_role_app_id": "78"
}
```

## POST /api/voiceover-drama/material-counts

请求：

```json
{
  "content_ids": ["q8V3Nofwxp", "6mijMyUX7A"]
}
```

响应：

```json
{
  "items": [
    {
      "content_id": "q8V3Nofwxp",
      "drama_name": "Veiled Dancer's Vengeance(Dubbed)",
      "series_code": "14511",
      "material_count": 23,
      "status": "ok"
    }
  ],
  "total": 1
}
```

## POST /api/voiceover-drama/filter

请求：

```json
{
  "content_ids": ["q8V3Nofwxp"],
  "roas_threshold": 45,
  "min_candidates": 15
}
```

响应核心字段：

```json
{
  "items": [
    {
      "target_content_id": "q8V3Nofwxp",
      "material_id": "2647829",
      "name": "xxx.mp4",
      "url": "https://...",
      "category": "cut mixed",
      "language": "hi",
      "duration": 0,
      "spend": 44.5,
      "roas": 6.58,
      "candidate_status": "substitute",
      "risk_label": "替补素材",
      "selected_by_default": true
    }
  ],
  "groups": [],
  "total": 1
}
```

## POST /api/voiceover-drama/design-tasks

请求：

```json
{
  "items": [
    {
      "target_content_id": "q8V3Nofwxp",
      "material_id": "2647829",
      "number": 1,
      "designer": "248",
      "end_date": "2026-05-31",
      "origin_name": 1,
      "description": "参考该素材风格扩展 1 个设计师任务。"
    }
  ]
}
```

后端会逐条素材调用外部接口。`end_date` 为空时不传给外部接口。
