# Dramawave AI 图片素材日报

用户确认全部 AI 后台正式图片，产出＋投放效果，每天北京时间09:00更新前一天；原始日快照保留，近7天趋势归因回补，固定COS链接。封面合成、YouTube及其他验证来源分开。

正式素材created_at（MySQL+08）作为入库日，原图时间另列，YouTube非published source_scope单列存量补传。正式ID去重，不计接收记录；无投放计产出，已删除保留效果。广告source_type=3/resource_type=1/resource_id，Dramawave1479及子应用，AF D0合计ROAS，零分母null。无法验证来源单列。

页面包含昨日产出与全部历史素材昨日效果、前日/大盘/同应用渠道其他图片、7日趋势、五维分组、明细筛选搜索分页CSV。负责人/优化师分开。首次补建initial不冒充09:00历史数据。

独立程序只读SQLite及63350 MySQL FIFO，不改业务API/上传/发布/数据库。日快照不可覆盖，固定COS入口最后发布；失败保留旧页，09:10/09:30重试，成功日期skip，flock排他。
