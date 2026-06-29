# 配音剧语种设计师任务

## 目标

在 AI 后台新增“配音剧语种任务”模块，支持按剧 ID 查询系列素材数量、按素材维度筛选候选素材，并对选中的素材批量创建设计师需求任务。

## 页面范围

- 批量素材数查询：输入多个剧 ID，后端先查 `ads_drama_info.series_code`，再按系列统计已有素材数。
- 筛选任务与配置：输入多个剧 ID、ROAS 阈值、保底候选数；不再填写目标语种、完成时间、任务名称。
- 候选素材筛选结果：列表颗粒度为素材维度，支持多选、导出筛选明细、创建任务。
- 批量创建弹窗：按每个选中素材分别展示设置模块，逐条填写任务数量、指定设计师、截止时间、是否使用原始素材名、任务描述。

## 核心口径

- 剧库来源：`ads_drama_info`。
- 系列字段：`ads_drama_info.series_code`。
- 素材来源：`ads_custom_resource_drama_insight.resource_id` 关联 `ads_custom_source.id`。
- 默认候选：ROAS 达到阈值的素材。
- 替补素材：当 ROAS 达标素材不足保底候选数时，按消耗从高到低补足；只有这类 ROAS 不达标补足素材打 `替补素材` 风险提示。
- ROAS 达标素材即使不足 15 条，也不打风险标签。
- 设计师下拉来源：`admin_role_apps.id=78` 对应的 `admin_role_users`，再映射 `admin_user_group.user_id`。

## 生成需求接口映射

外部接口：`POST https://ads-admin.static.kunlun.com/api/ai/kol-task`

服务端环境变量保存 Bearer token，不进入前端、文档或 GitHub。

字段映射：

| 外部字段 | 来源 |
| --- | --- |
| `name` | `AI_{产品名称}_{剧语言缩写}_{剧ID}_{日期}_{需求人}_随机数` |
| `type` | 固定 `11` |
| `content_id` | 目标剧 ID 拼接后的完整字符串，格式：`包名#-#剧ID`，例如 `com.dramawave.app#-#0QjVjIe9MG` |
| `name_keyword` | 目标剧名称，例如 `Yeraltı Kralı'nın Dönüşü(Dublajlı)` |
| `app` | 产品设置表产品 ID，例如 Dramawave 为 `1479` |
| `country` | 目标剧 ID 对应 `ads_drama_info.country` |
| `language` | 目标剧 ID 对应 `ads_drama_info.language` |
| `number` | 弹窗内该素材填写的任务数量 |
| `category` | 选中素材的 `ads_custom_source.category` |
| `designer` | 选中设计师的 `admin_user_group.user_id` |
| `user_id` | 当前需求人的 `admin_user_group.user_id` |
| `is_ad_activity` | 固定 `0` |
| `examples` | 选中素材 URL 数组 |
| `introducation` | 弹窗任务描述 |
| `end_date` | 截止时间；为空不传 |
| `tag` | 目标剧 ID 对应剧名 |
| `origin_name` | 默认 `1`，表示保持上传文件名 |

不传字段：`content_ids`、`example`。
