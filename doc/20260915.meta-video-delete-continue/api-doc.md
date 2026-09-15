# 接口与结果兼容

接口路径、请求参数、普通执行确认、分页和phase顺序保持。DELETE /act_{account_id}/advideos?video_id={video_id}使用来源投放User Token。

对明确无效Video ID的DELETE失败新增后置核实：完整读取该账户库，确认缺失返回already_deleted，现有账户结果增加delete_error与verification；顶层保留confirmed_absent和精确proof。Store使用既有账户receipt，不生成全局Video成功receipt。

若核实仍在库/不可读/未完整，保留failed及原错误，message展示核实说明；后续对象继续。读取不发出DELETE，原DELETE超时仍unknown。手动重试流程不变。
