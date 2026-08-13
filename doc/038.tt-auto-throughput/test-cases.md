# 测试用例

| 编号 | 场景 | 预期 | 优先级 |
| --- | --- | --- | --- |
| TC-01 | 模板 120 秒、账号 60 秒 | 选择上限 60，GPU prepare 0 次 | P0 |
| TC-02 | 任务距发布 1 小时、提前窗口 2 小时 | 可领取 selection/prepare | P0 |
| TC-03 | TC-02 成片 ready、尚未到点 | publish lane 无法领取 | P0 |
| TC-04 | TC-02 到达 scheduled_at | publish lane 可领取 | P0 |
| TC-05 | runner 3 workers | 2 个 prepare lane，1 个 publish lane | P0 |
| TC-06 | GPU 新制作与复用 | 8 个阶段耗时一致返回 | P1 |
| TC-07 | 旧 manifest 无计时字段 | 继续读取 | P1 |
| TC-08 | 提前窗口跨北京时间午夜 | 次日窗口内 slot 可创建 | P1 |
| TC-09 | Creator Info 失败 | 选择瞬态失败，未制作、未发布 | P0 |
| TC-10 | 全量相关回归 | 无失败 | P0 |

不执行真实 TikTok 发帖；生产验证使用 health、离线请求和下一次自然调度事件。
