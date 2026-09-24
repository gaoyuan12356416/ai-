# 代码审查

审查范围：features/youtube_analytics、独立GET路由、共享导航新增项、可视化页面、部署/回滚。

- Cookie鉴权在任何数据读取前执行；沿用模块和导航权限。来源缓存不含响应权限，每次按最新服务端actor重新过滤，options/report/export均后端限定。
- 冻结归因二元组全局唯一性先判再过滤用户；不能把跨用户冲突误认成自己的唯一记录。源表不按channel过滤，SQL按日期与二元组先聚合。
- source只读固定63350/site2284，SQL日期规范化，原始进程错误不外泄；5分钟8范围缓存且查询互斥，异常不缓存成0。
- 报表模块未导入写入store或publisher；SQLite URI mode=ro/query_only、一致性事务及读取上限。
- 自动/手动/历史候选已覆盖，未证明租户归属不分配。未匹配数据不跨租户披露。
- 金额整数美分合计；转化率按合计计算；无日数据null和CSV说明已修正BUG-001；不伪造YouTube曝光/播放。
- 前端独立受权限API，动态文字转义，CSV服务器端公式防护，全部响应no-store。
- 上线hash拒绝漂移，备份原代码/导航和在线SQLite；仅API启停，回滚保留全部事实数据。

实际测试与生产验收见test-report.md和deployment-evidence.md。
