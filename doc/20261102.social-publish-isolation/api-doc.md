# 接口兼容
手动 plan 内部接口可选 preflight_failures 数组：account_id/material_id/error_code/error_message。候选和失败项必须精确分割原冻结配对；失败账号不能被换素材或替换账号。缺省参数保留原严格完整计划契约。auto_template 不接受部分计划。

手动 run 响应新增 preflight_failures。失败项落 x_post_manual_preflight_failure，主键 (manual_run_id, account_id)。计数纳入 failed_count；queued_count 为实际队列数；原 expected_count 不变。
