# 验证记录

2026-11-04：Python 24/24 通过；真实 Chromium 本地模拟接口测试通过（含四类数字、旧 DTO 提示、异常原因、管理员/普通用户、未登录、容量表单、第三节点、15 秒刷新、移动端与旧深链）。截图已查看，work/pipeline-monitor-ui 为测试数据不提交。

北京时间 10:17:21 线上只读候选验证：summary 0.106 秒；待执行 75、执行中 14、预约中 68、异常 278。四类合计 435，等于同一快照原 queued+ready+preparing+publishing+failed+unknown。查询计划为 SEARCH pipeline_job USING INDEX pipeline_job_channel_status_time；无警告。容量版本 104；未写数据库、未触发平台请求。

异常包括所选时段失败、原待核对任务、受未知结果账号锁阻塞的到期任务以及显式 hold、近期反复领取。上线后的数量会随正常业务变动，不以此快照作为永久值。

部署完成后以独立 deployment-result 记录提交、备份、HTTP 排空证据、配置一致性和公开静态资源校验。本次不修复或重发异常任务。
