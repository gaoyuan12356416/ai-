# 并行实现接口合同

公共前缀/api/fb-video-publish，沿用Cookie/模块导航/同源/4MiB。修改此合同需通知并行开发者。

## 逐素材预约

新版/preflight字段：schema_version=2,operation_id,material_ids,page_ids,pairs,processing_mode,material_schedules。
material_schedules为{素材ID:null或含时区ISO时间}，完整覆盖所选素材；浏览器按北京时间转UTC。
pairs保持原material_id,page_id,config_instance_id,config_version,caption_template。
/batches加preview_token,allow_repeat。新请求不带旧全局三个时间字段；旧协议仍可用。
预检items增加schedule_kind=immediate|scheduled，继续返回planned_at_utc。新任务间隔0，旧任务按原快照。
GET /pages新增created_by_name（可信快照/同租户解析/ID兜底）。

## Page素材池

所有写入带config_instance_id,expected_version。GET无池返回version=0,status=paused,times=[]，不写库。
写后返回pool。pool字段：config_instance_id,version,status,times,processing_mode,publisher_user_id,publisher_name,pause_reason。
status=paused|enabled|archived；times为HH:mm数组。默认processing_mode=original。

- GET /pages/{id}/pool?state=pending|processing|published|failed|slots&cursor=&limit=50 → {ok,pool,items,counts,next_cursor,has_more}，最多100。
- item至少含id,material_id,material_name,drama_name,language,status,position,created_at_utc,task_id,batch_id,error_message,language_mismatch,can_view_task；id视为不透明字符串。
- item状态pending|processing|published|failed|cancelled|removed，后两者可放异常/历史。无权查看任务时不泄露冻结文案/链接/其他发布人身份快照。
- POST /pages/{id}/pool/settings：身份版本字段+times,processing_mode；首次暂停，普通保存不换归属，时间从保存后生效。
- POST /pages/{id}/pool/enable或/pause：仅身份版本字段。enable使用当前actor，复验待发布项语言和Page可用。
- POST /pages/{id}/pool/items/preview：身份版本字段+material_ids，只读 → {ok,preview_token,repeat_required,items}。
- POST /pages/{id}/pool/items：以上字段+operation_id,preview_token,allow_repeat；幂等、每次最多200项，池内同素材不可有多个未结束项。
- GET /pages/{id}/pool/operations/{operation_id}：查询本人的入池操作，支持响应丢失恢复。
- POST /pages/{id}/pool/remove：身份版本字段+item_ids，仅未绑定项。
- POST /pages/{id}/pool/move：身份版本字段+item_id,direction(top|up|down)，仅未绑定项，服务端算顺序。
- 时点items含slot_at_utc,status,reason,material_id,task_id；任务详情沿用原权限。

所有409保留输入提示刷新；明确拒绝accepted=false，网络模糊结果保留operation_id查询。

## 内部身份和调度

主API POST /internal/fb-video-publish/identity：{tenant_key,user_id} → {ok,actor}，仅loopback+内部Bearer/拒绝转发头，公网nginx拒绝。每次解析可信当前身份，不能借客户端归因。
主API在GET pages结果内按当前租户批量解析历史创建人。
领域实现提供PoolService和PoolScheduler；具体构造签名由领域与集成开发直接同步。
独立scheduler只调用sidecar内部pool-start/pool-tick，不直连MySQL、不调用Graph。
