# Meta Video 按广告账户删除

用户已验证 DELETE /act_{account_id}/advideos?video_id={video_id} 使用实际投放Token成功，明确要求统一改为该路径。该最新要求替代此前Video节点DELETE及Page凭证选择规则。

## 规则

- Creative→Ad→Video阶段顺序、原Cookie/模块/产品ACL、固定预览和普通确认保持。
- Video仍按ID去重显示，按冻结的account_id + video_id分别执行账户素材删除。结果只证明所列账户处理成功，不证明全局视频或Page帖子消失。
- 新预览冻结source_row_id/ad_id/product_id/account_id/source user_id关系；优先动态读取实际投放用户的User Token。旧预览仅依据冻结ad_ids/产品/账户读取来源恢复凭证，不追加删除对象；恢复失败沿用原冻结候选Token。每个DELETE仅一种凭证，不自动轮换重写。
- 不添加Meta账户、Video、Page、Creative或范围外引用读取门禁；Page Token不用于当前Video删除路径。缺失Token/账户是无法构造请求的明确失败。
- 同一视频多账户逐个记录；单账户失败/未知继续其他账户及对象。成功pair跨任务跳过，失败手动重试，未知先只读核实。一个账户成功不能授权跳过另一账户。
- 台账先落盘后DELETE；重启保留进度，需要人工恢复；业务源MySQL只读。历史成功、旧Video节点删除结果与旧Post任务不改写。

## 验收

精确端点/参数/来源凭证、多账户部分失败重试、跨任务去重隔离、未知与重启恢复、审计失败停止、普通确认/分页/安全诊断均需测试。线上验收只读，不以真实DELETE作为部署测试。
