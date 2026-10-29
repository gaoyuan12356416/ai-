# 增量API契约

基址/api/video-editing，原Cookie、模块权限、同源JSON规则不变。

- GET /catalog/resources?q=：items[{series_code,english_title,english_title_status}]，ID精确优先、英文名前缀，最多50资源。
- GET /catalog/resource-variants?series_code=：items[{app_id,content_id,series_code,title,language,country}]；ID置查询串支持特殊字符。
- POST /uploads：原字段+必填library_kind(material/free_episode)、series_code。已有会话按原操作身份核实，无需补字段重传。
- GET /assets：新增library_kind、series_code筛选。Source DTO增加资源标签、栏目、preview_geometry。
- PATCH /assets/{id}：可分别改name、series_code、library_kind；仅上传者/管理员。
- POST/PATCH /projects：新增schema_version=2与units；每unit{id,series_code,title,clips}。clip除原字段增加mirror、subtitle_band(null或{top,bottom})。响应issues按单元给出提示，未完成编辑可以保存。旧v1读写继续兼容。
- GET /projects/{id}：v1增加只读upgrade_units；v2增加issues，不自动改写。
- POST /projects/{id}/render：原operation_id、expected_version+unit_ids（省略表示全部）。响应items任务、results[{unit_id,status:accepted/reused/invalid,task_id?,code?,message?}]、issues。操作重放使用原参数；已接受单元按项目版本+unit_id复用。确认收到局部失败后用新操作号重试对应单元，网络未知结果沿用原操作号。
- POST /tasks/{id}/material-source：v2首次入库需app_id/content_id；已存在账本优先复用。v1空请求兼容。

新task DTO增加unit_id、series_code、english_title。新任务冻结source_attribution；素材改标签不改冻结归属。新增SQLite ve_unit_render唯一键(project_id,project_version,unit_id)，历史表和记录保留。

GPU task_type保持video_editing_v1。recipe.version=2表达镜像；v1规范化与指纹不变。profile独立表示既有旧/紧凑导出配置，两版均兼容。
