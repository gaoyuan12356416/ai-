# API 文档

## 接口列表

本需求不新增 HTTP API。新增内部运维 CLI：`scripts/fb_auto_post_targeted_backfill.py`。

## 请求/响应

validate-only 示例：

```bash
python3 scripts/fb_auto_post_targeted_backfill.py \
  --source-run-id 20 \
  --expected-source-planned-at-utc 2026-08-25T00:44:00+00:00 \
  --expected-beijing-date 2026-08-25 \
  --page-id 1009871948881047 \
  --page-id 1014456238423538 \
  --page-id 1069327366257056 \
  --page-id 761697440365789 \
  --page-id 957642277435629 \
  --operation-id 20260825-run20-missing5
```

真实建单需在相同参数后增加：

```bash
--apply --expected-fingerprint <64位dry-run指纹> \
--report-path /mnt/data-disk/fb-auto-post-publisher/recoveries/<新文件>.json
```

成功 dry-run 返回 `status=validated`、来源/目标/实时凭证计数和 `fingerprint`。成功 apply 返回 `status=created`、`run_id`、5 个脱敏任务摘要；重复同操作号返回 `status=already_created` 和相同 `run_id`。输出不含 Token、消息、媒体 URL 或长短链。

## 错误码

| 错误码 | 含义 |
| --- | --- |
| `fb_auto_backfill_source_mismatch` | 来源运行、时间或今天日期不匹配 |
| `fb_auto_backfill_source_scope_mismatch` | Page 集合不等于来源全部缺凭证 Page |
| `fb_auto_backfill_source_not_pristine` | 来源任务已有尝试、账本或 unknown |
| `fb_auto_backfill_template_changed` | 模板停用、版本或冻结配置变化 |
| `fb_auto_backfill_page_scope_changed` | Page 已移出模板范围或当前范围重复 |
| `fb_auto_backfill_already_exists` | 同一来源已有其他回补操作 |
| `fb_auto_backfill_target_already_attempted` | 来源时隙后目标 Page 已有发布尝试 |
| `fb_auto_backfill_fingerprint_changed` | dry-run 后实时状态漂移 |
| `fb_auto_backfill_live_gate_closed` | 预制或真实发布门禁未开启 |
| `fb_auto_backfill_report_path_invalid` | 审计路径越界、不安全、已存在或非 JSON |
| `fb_auto_previous_run_backlog` | 已有到期任务未完成 |

## 兼容性说明

现有后台/API、自动 due-slot 和整模板 run-now 参数不变。`create_run` 新参数均为可选；只有提供 `target_page_ids` 时才强制 `manual + required_template_version`。
