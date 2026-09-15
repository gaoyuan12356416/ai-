# 接口与数据契约

外部preview/execute/reconcile路由及执行参数保持；前端不能提交任意账户、Video、Page或Token。

Video当前写请求：DELETE /act_{account_id}/advideos，query参数video_id为冻结Video ID，Authorization使用对应投放User Token。官方SDK对应AdAccount.delete_ad_videos：[Meta SDK](https://raw.githubusercontent.com/facebook/facebook-python-business-sdk/main/facebook_business/adobjects/adaccount.py)。

新预览内部字段video_account_sources:[{account_id,source_row_id,ad_id,product_id,user_id}]。该字段不返回前端。每个目标(account_id,video_id)独立审计，父对象仍为video:{id}。

公开结果delete_mode/delete_scope='ad_account_video'；account_results和video_account_results包含逐账户status/result。诊断保留delete_account_id/delete_endpoint/credential_kind/credential_user_id/credential_relation及安全错误字段，不保留Token。

新增SQLite video_accounts及video_account_attempts表。成功回执key为video_account:{account_id}:{video_id}；旧全局video:{id}回执只代表以前的对象删除结果。account模式绝不写新的全局回执。

未知核实只GET当前账户advideos完整分页；明确缺失证明需confirmed_absent=true及匹配account_id/video_id、complete=true的proof。存在、权限失败、分页不完整或超限保持unknown。账户证明不会传播为其他账户的成功。
