# 接口与数据

执行接口仍接受preview_id/phases/request_id；用户不能提交Page、Token或新Video ID。无schema迁移，无业务源写入。

GraphClient可选video_credential_provider由生产Bridge绑定SqlSource.video_credential。prepare_video_delete(obj)返回仅在进程内使用的(token,safe_context)，Service先record_object_credential，再delete(obj,prepared=...)，防止审计与实际Token二次选择不一致。

safe_context字段：delete_mode、credential_kind、credential_page_id、credential_row_id、credential_fb_user_id、credential_user_id、credential_relation、credential_lookup、credential_lookup_message。API现有result/detail承载这些安全字段。Token/page_access_token等字段禁止入台账。

credential_relation：
- video_from：Video返回的身份，选择相应Page或精确匹配的候选User。
- creative_page：冻结Creative精确包含该Video的关联Page，仅为凭证选择依据。
- configured_user：未解析到匹配凭证，沿用原候选User Token。

Page查询：page_id精确匹配；p.fb_user_id与f.facebookUserID二进制相等；f.user_id限定原冻结候选；p.status<>1且Token非空；每次重新读取。元数据缓存只保存Video/Creative/Page关系，不保存PageToken。

审计object_credential_selected在DELETE之前提交；重复相同记录幂等，同一attempt不能更换身份。未知结果不允许重复DELETE；核实接口只GET。
