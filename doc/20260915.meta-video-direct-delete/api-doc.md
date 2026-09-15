# API

前缀/api/fb-post-ad-delete；Cookie、fb_ad_asset_delete、当前产品 ACL 和关闭的旧Post接口保持。

## GET /jobs/{job_id}

新增 video_direct_eligible_count（历史blocked Video数量）；objects[].video_direct_eligible仅这些对象为true。原status/reason/result用于历史审计。页面将其纳入可执行。

status=pending包含历史可直接执行Video；status=blocked排除它们。total及分页按此筛选；summary/phase_results仍保留台账计数，由页面新增字段投影，避免重复计数。

## POST /jobs/{job_id}/execute

请求字段保持preview_id/phases/request_id；job_id来自路由。无需direct/override参数。拒绝追加objects/object_ids/ad_ids/creative_ids/video_ids，服务端决定阶段顺序。

Video pending/failed/blocked可领取；deleted/already_deleted/unknown/in_progress跳过。全局回执和锁依然有效。尝试/锁提交后单次DELETE /{video_id}。旧blocked原因写领取审计。

## 只读接口

recheck只核验Creative/Ad，Video不参加。reconcile(Video)使用已有Token直接单次GET Video节点，无账户GET。读取失败、仍可读取均不证明已删除，unknown不放开重试。

## Meta结果

true或success=true→deleted；明确错误→failed；超时、连接中断、不明确响应、5xx→unknown。记录安全错误字段、credential_user_id及delete_mode=video_id_direct，不记录Token。失败不自动轮换凭证。
