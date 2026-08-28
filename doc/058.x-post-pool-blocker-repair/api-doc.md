# API契约

- 内部POST /internal/posts/accounts/{id}/verify：schedule_preflight=true时先读取发布账本，遇账号历史未确认或明确锁定返回409和具体x_post_account_needs_review/x_post_account_locked。普通verify/manual调用不改变行为。
- 内部POST /internal/posts/drama-pool/available：可携带configured_account_ids用于未完剧归属检查；account_ids仍为本次参与候选账号。最终建队只接受冻结run配置内的有序子集。
- 内部POST /internal/posts/schedule-plan：已知ServiceError增加outcome_known=true、unknown_outcome=false；未知异常或丢响应保持未知，不重复写计划。
- 账号隔离不开放任何公共发布/恢复接口；既有Cookie/admin门禁不变。
- 新排期部分容量仍error_code为空，error_message记录具体被跳过账号原因，完整account_ids不删改。
