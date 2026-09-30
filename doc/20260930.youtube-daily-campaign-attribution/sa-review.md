# 方案审查

通过：复用 AI 后台 b37156a 的只读 catalog 和唯一 campaign resolver，避免两套不同归属规则。候选集在全局建立，再按 tenant/owner 统计；仅已发布短链可进入 catalog。

发送风险：旧版已发，不能清除旧 sent 状态绕过去重。采用固定更正版本、独立账本文件和 UUID namespace，共享运行锁；unknown/sending 继续阻止重试。补发必须指定日期且效果数据可用。

发布量快照与冻结 catalog 各自只读；不写业务库，不登录 YouTube，不修改频道归属。已有发送回执须读回核实。
