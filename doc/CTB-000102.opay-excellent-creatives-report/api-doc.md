# API 与数据契约

## 共同约束

- 基础路径：`/reports/opay-excellent-creatives/`。
- 公开只读，无 Cookie/Token 要求。
- HTML 和 `latest.json` 使用 `Cache-Control: no-store`；版本化 JSON/缩略图可长期缓存。
- 所有响应带 `X-Robots-Tag: noindex, nofollow`；HTML 同时包含 robots meta。
- 金额单位 USD，时间使用 ISO 8601 北京时区或 `yyyy-mm-dd`。

## 接口列表

### `GET /reports/opay-excellent-creatives/`

返回静态 HTML。无结尾 `/` 的路径 301 到规范路径，不跳转飞书。

### `GET /reports/opay-excellent-creatives/latest.json`

公开提交清单：

```json
{
  "schema_version": 1,
  "data_version": "20260826T120000000000+0800",
  "generated_at": "2026-08-26T12:00:00+08:00",
  "latest_month": "2026-07",
  "months": [
    {
      "month": "2026-07",
      "stage": "final",
      "generated_at": "2026-08-05T10:00:00+08:00",
      "row_count": 26,
      "status": "success"
    }
  ]
}
```

`latest.json` 只在该版本全部月份文件和 HTML 准备完成后原子替换。

### `GET /reports/opay-excellent-creatives/data/<version>/<yyyy-mm>.json`

顶层字段：

- `schema_version`、`month`、`stage`、`generated_at`、`keyword_config_version`。
- `rows`：入选素材；无优秀素材不含占位行。
- `benchmarks`：六个渠道/App 基准。
- `audits`：六个渠道/App 计算状态、映射覆盖和排除原因。
- `stage_diff`：相对该月初版/上一可见版的行数和素材 ID 差异。

素材行核心字段：

```json
{
  "month": "2026-07",
  "channel": "Meta",
  "app": "NG OPay",
  "custom_source_id": 4566082,
  "material_type": "VID",
  "thumbnail_url": "assets/thumbnails/4566082-....jpg",
  "source_url": "https://...mp4",
  "source_status": "available",
  "spend": 8000.0,
  "impressions": 4000000,
  "clicks": 20000,
  "installs": 1490,
  "af_d0_first_transactions": 234,
  "maker": "胡杰",
  "first_launch_date": "2026-05-13",
  "first_launch_source": "platform_success",
  "selling_points": [],
  "selling_point_status": "pending",
  "selection_rule": "A+B",
  "evidence": {}
}
```

`evidence` 至少包含素材/平台 CTR、CPA、消耗排名、累计占比、是否在前 50%、A/B 判定、映射覆盖和数据质量说明。无穷 CPA 以 JSON `null` 表示并由 `*_cpa_finite=false` 解释，禁止输出非标准 `Infinity`。

### `GET /reports/opay-excellent-creatives/assets/thumbnails/<file>`

返回同源缓存 JPEG/PNG。文件名由素材 ID 和内容哈希组成，不接受目录穿越。

## 错误码

- 静态文件不存在：404。
- Nginx/发布异常：5xx；正常生成失败不切换清单，因此用户继续看到上一成功版本。
- CLI 以非零退出码表示配置、读取、计算、冻结或发布失败，并输出不含凭据的 JSON 错误摘要。

## 兼容性说明

- `schema_version=1` 内只允许新增字段；删除/改义需升版本。
- 前端忽略未知字段，缺失缩略图/源文件字段时显示降级状态。
- 旧 AI Game Performance 路径和鉴权契约不受影响。
