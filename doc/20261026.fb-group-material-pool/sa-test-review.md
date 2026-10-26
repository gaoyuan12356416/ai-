# 用例评审

必须验证真实SQLite事务/唯一约束、Worker执行前复检、来源SQL通过broker与新旧identity入口分离。所有Meta写入替身测试；线上只读并保留已有queued/unknown。实际结果以test-report为准。
