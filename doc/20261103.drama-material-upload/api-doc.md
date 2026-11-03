# API

GET /api/drama-material/jobs/{job_id}/material-source 返回 {items:[{material_kind,source_url,status,error,material_id?}]}。

POST 同路径，JSON {"material_kind":"concat_video|no_bgm_video|random_template"}，202返回 {item:{status,error,material_kind,material_id?}}。重复提交返回原记录。身份来源为当前Cookie会话；不接受客户端指定产品、URL、人员。

未登录401，无模块/对象权限403，非JSON415，未完成/产物不合法409，错误类型400，依赖暂不可用503。任务列表及详情DTO新增material_source_uploads，读取失败为null且不影响原任务展示。

本地账本 /mnt/data-disk/drama-material-source/upload.sqlite3；成功必须有效接收回执、唯一正式素材字段核对及回执source_type=3/source_id匹配。结果不明不再POST。
