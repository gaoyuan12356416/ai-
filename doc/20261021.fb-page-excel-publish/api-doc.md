# Excel 与素材 ID 入池 API

公开前缀 `/api/fb-video-publish`。需要飞书 Cookie、fb_video_publish 模块和 fbPageManagement 入口；身份由主 API 可信解析，前端不提供 Token 或操作人。

## Excel

| 方法/路由 | 请求 | 结果 |
| --- | --- | --- |
| POST /excel/preview | operation_id, filename, content_base64 | 只读解析预检 |
| POST /excel/batches | request, preview_token, allow_repeat | 原子创建有效集合 |
| GET /excel/operations/{operation_id} | 无 | 当前操作人的原操作结果 |
| GET /excel/batches/{batch_id} | 无 | 权限内 Excel 批次 |

预检返回 ok、request、preview_token、expires_at_utc、items、skipped、summary(total/valid/skipped)、repeat_required。前端原样保存并回传完整 request，不自行拼装或修改。

仅接受 `.xlsx`、2 MiB、200 个非空数据行，首个工作表表头必须为规定八列。全部无效时 request=null、items=[]、preview_token=""，仍返回逐行原因。公式（含八列外）、额外非空列、疑似数值 ID 精度损失明确报错；压缩结构、解压大小、最大行号和 200,000 扫描单元格限制防止异常工作簿消耗过量资源。

标准化 request 含 schema_version=3、operation_id、material_ids、page_ids、pairs、processing_mode、excel。pair 含原配置实例/版本、caption_template 及 row_number、processing_mode、credential_mode、title、scheduled_at_utc；excel 含 filename、total_rows、skipped。汇总 processing_mode 可以 mixed，任务只使用 original/random_overlay。

items 含原工作表行号、Page/素材 ID 与名称、caption、具体加工方式、credential_mode(page_pool/operator)、title、planned_at_utc、schedule_kind、repeat_required。skipped 含 row_number、page_id、material_id、code、message。

allow_repeat=false 在完整签名校验后跳过历史成功行；无剩余则 accepted=false，不建空批次。接受返回现有 batch_id/operation_id/batch，任务 DTO 增加行号、标题和策略，批次包含 Excel 元数据。摘要绑定原 request 与 allow_repeat，同 ID 不能更换内容或确认选择。既有普通手动入口拒绝 schema 3；Excel 查询不能返回普通手动批次。

## 素材池

新增 POST /pages/{page_id}/pool/items/resolve，请求为 config_instance_id、expected_version、material_ids（去空行后的原序字符串列表，最多 200 条）。

返回有效唯一顺序 material_ids、items（素材信息及 repeat_required）、skipped（输入行号/ID/错误）、summary、preview_token、repeat_required。无有效项时无可提交预检令牌，不创建池记录。

resolve 对有效集合签署与现有 /items/preview 相同的事实，随后调用现有 /items。若未确认历史重复，先对剔除重复后的子集重新调用严格 /items/preview，再添加；不允许旧集合 token 提交子集。现有严格 preview 和 operation 恢复接口不变。

## 失败与恢复

- 业务逐项错误放 skipped；文件结构、权限、SQL、签名或版本错误整体失败。
- 预检 15 分钟有效，提交复验来源、配置、占用、历史及额度。变化后保留输入重新预检。
- 仅首次明确 accepted=false 才可释放表单；网络断开、非 JSON、响应过大保留原操作查询/按原请求重试。后续拒绝不能证明先前未知请求未成功。
- 已接受操作优先回读，不依赖预检仍有效。不得返回 Token、数据库凭证或内部 bearer。
