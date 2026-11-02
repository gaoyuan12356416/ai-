# SocialKit 报表接口

## 认证与读取边界

前缀 `/api/socialkit-analytics/`。GET接口使用现有Feishu Cookie登录，并检查独立 `socialkit_analytics` 模块和对应导航权限。管理员及获授权用户可查看报告的全部团队数据。接口只读取已发布的本地JSON/SQLite快照，不启动采集或访问远端源库。

## 路由

| 路由 | 用途 |
|---|---|
| `options` | 返回当前版本可用日期、维度及过滤选项 |
| `report` | 返回总览/效果/发布视图的总计、分组、图表与分页数据 |
| `export.csv` | 按同版本和同筛选导出CSV |

`options` 只接受可选的 `version`；`report` 和 `export.csv` 接受下表参数。所有日期端点均包含在范围内。

| 参数 | 规则 |
|---|---|
| `start/end` | `YYYY-MM-DD`，最多 366 天；缺省为当前快照截止日及之前 13 天 |
| `view` | `overview`（默认）、`effects`、`publications` |
| `group_by` | 最多 3 个不同白名单维度，逗号分隔或重复参数；默认 `platform` |
| 公共维度 | `date,team_id,product,platform,account,drama`；除 date 外可重复筛选值，同维度 OR、不同维度 AND |
| 收入专用维度 | `country,device_os,material_type,creation_source`，只供 effects |
| 发布专用维度 | `language,account_country,reason,task_status`，只供 publications |
| `search` | 按账号名称、剧目名称或剧目 ID 搜索，最多 200 字符 |
| `sort/direction` | 当前分组或当前视图指标排序；方向 `asc/desc`（默认 desc） |
| `ranking_metric` | TOP 10 图表指标；默认收入视图 revenue、发布视图 success |
| `page/page_size` | 页码从 1 起；每页 20、50 或 100（默认 50） |
| `version` | 固定 current 或 previous 中已发布的版本，不接受暂存快照 |

产品、账号及剧目筛选值使用 options 返回的 `value`，不以显示名称作为身份。CLI、页面和 API 共用 `query.report`；未知参数和跨视图维度返回 400。

返回元数据包括版本、日期范围、源更新时间、报表更新时间和归属质量。金额来自整数分，不使用浮点累计。固定version已不可用时明确返回错误，不悄悄切换当前版。缺失日期和未生成快照不得返回伪造的零数据。

`report` 响应包括 `meta,group_by,totals,trend,ranking,breakdowns,rows,pagination,parameters,metric_labels,dimension_labels`。`meta.partial/missing_days` 表示未覆盖日期；趋势缺失值为 null，已成功采集且无记录的日期为 0。`meta.refresh_status` 保留最新采集状态，失败时仍返回上次完整快照并标记 `stale`。服务端内部 `all_rows` 不进入 JSON 响应；CSV 使用全部分组行而非当前页。

认证失败为 401，权限不足为 403，不支持方法为 405，固定版本过期为 409，当前快照尚未准备为 503。页面“刷新”仅重新请求已发布版本。CSV 为 UTF-8 BOM，转义公式样式的维度名称，并包含版本和两种日期口径。

## 只读CLI

```
python -m features.socialkit_analytics.query --data-root /mnt/data-disk/socialkit-analytics --start 2026-10-19 --end 2026-11-01 --view effects --group-by platform --format json
```

可重复 `--filter key=value`，支持 `--version`、`--page`、`--page-size` 和 `--format csv`。技能的query_report.py通过已配置SSH helper调用此入口，不复制查询口径，不包含数据库凭据。
