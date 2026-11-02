# 接口
Cookie网关：GET /api/pipeline-monitor/summary|workers|tasks；管理员PUT /api/pipeline-monitor/capacity、POST /api/pipeline-monitor/nodes/enroll。actor来自现有会话；分页使用游标；缺失测量为null。
节点POST /internal/pipeline/nodes/enroll|register、workers/heartbeat|drain、jobs/claim|heartbeat|begin-operation|checkpoint|complete|fail。身份为job_id/generation/claim_token/node_id/worker_id；平台操作另绑operation_id/request_sha256/account_resource。
原生桥 /internal/pipeline-dispatch/{x,fb,tt,youtube}/{reserve,begin-operation,checkpoint,complete,fail,reconcile,pause}。节点不直接开渠道数据库。pause只插入精确未执行任务hold，拒绝在途/未知。
collector增量扫描，事务内维护计数快照。页面不调用平台、不重建全量统计；最终发布ID是唯一成功事实，转发源帖不另计目标成功。
