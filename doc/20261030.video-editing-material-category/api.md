# API 增量：material_category

现有 POST /api/video-editing/tasks/{task_id}/material-replication 增加可选字符串 material_category。精确可选值：

| 值 | 前端显示 |
|---|---|
| 解说 | 解说 |
| 二创 | 二创 |
| 混剪 | 混剪（新表单默认） |
| 精剪 | 精剪 |
| 切片 | 切片 |
| image | 图片（image） |
| Dramawave Post | Dramawave Post |

显式提供 null、空串、错误大小写、前后额外空格或非上述值均拒绝。省略时保持字段缺失，不注入当前默认值。新表单显式发送选定值；worker 透传冻结请求，无额外改写。

DTO 记录增加 material_category；历史缺字段返回 null。任务卡、提交摘要、受理记录、未决请求及冲突展示均区分明确分类与旧记录未指定。

B 去重键仍为 URL + audio_type + task_type + 规范化 target_langs，分类不加入。相同 B 键分类不同（含旧缺省 vs 新显式）返回 reused/conflict/conflict_fields，previous 可为 null；仅展示既有任务，requested 新分类不生效。旧 operation_id 无分类重放保持原摘要可复用；给同 operation_id 追加分类属于参数变更，返回 409。

权限、token 请求头、源素材身份、未知结果不重发及仅受理历史的边界不变。
