# API 文档

前缀 `/api/fb-video-publish`，仅飞书 Cookie；`fb_video_publish` 权限和对应导航均须可用。
写入必须同源 application/json、单 Content-Length、最多 4 MiB。
完整 JSON 示例与内部适配器定义见 [implementation-contract.md](implementation-contract.md)。

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | /languages | 剧语言选项 |
| GET | /pages/available?q= | 当前负责人可访问的 DramaWave Page 候选 |
| GET / POST | /pages | 配置列表 / 批量创建 |
| POST | /pages/{PageID} | 实例和版本 CAS 编辑 |
| POST | /pages/{PageID}/delete | CAS 删除模块配置 |
| GET | /materials?q=&language=&cursor=&limit=50 | 有界主键分页，50默认、100上限 |
| GET / POST | /draft | 当前 tenant/user 的草稿 |
| POST | /preflight | 无副作用、15分钟签名摘要、逐配对占位文案和长链字段 |
| POST | /batches | operation_id 幂等整批冻结 |
| GET | /operations/{operation_id} | 原操作结果核对，不要求现有Page配置/邮箱映射 |
| GET | /batches?limit=&cursor= | 精简批次列表 |
| GET | /batches/{batch_id} | 包含逐任务冻结快照 |
| GET | /tasks?limit=&cursor=&status= | 有界任务列表 |
| GET | /tasks/{task_id} | 任务详情 |
| POST | /tasks/{task_id}/cancel | 取消尚未向 Meta 发送的任务 |

批次输入只含选择、配对、文案、配置实例/版本、排序及时间设置；
不得以浏览器传入 actor、email、sub_user_id 或归因覆盖服务端事实。
原片 `processing_mode=original`；随机加工 `random_overlay`。
时间为带时区 ISO8601，UI 按北京时间转换。单批最多200配对。

成功响应含 `ok:true`；明确校验失败含 `ok:false,code,message,accepted:false`。
网络超时、无效响应等未知结果不得附 `accepted:false`，客户端保持原操作标识并先查询结果。
409 表示配置/来源变化、旧预览、重复事实更新、额度或活动占用冲突；保留用户输入。
401 要求 Cookie，403 权限/同源失败，404 对无权访问的批次/任务不泄露存在性。

素材列表只需登录/模块/导航，不产生归因；Page范围依赖当前有效负责人映射。
预检与提交都重新检查发布人唯一有效邮箱映射，管理员同样执行。
取消/查看原任务不依赖后来是否仍有该Page范围或邮箱映射。

旧 `/api/admin/fb-auto-publish`、旧模板/冷却/2049短链均保留；
SQL trigger + Python事务门禁以真实 Page ID 跨租户/入口执行。
