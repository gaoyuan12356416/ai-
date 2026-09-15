# QA用例

| 场景 | 预期 |
| --- | --- |
| Ad/Creative GET失败或源警告 | 源匹配Video仍冻结并可执行 |
| 范围外引用、索引不可用/过期 | Video无此类查询，直接DELETE |
| 历史blocked Video | 普通确认即可领取，保留旧阻止审计 |
| 成功、跨任务in_progress/unknown锁 | 不重复写 |
| 权限失败、超时、5xx、模糊响应 | failed/unknown，不假成功、不自动换Token |
| 单对象失败与多阶段 | 继续执行，Creative→Ad→Video |
| 无Token | 明确失败，无Graph调用 |
| 权限撤销、ID注入、旧preview、重复request | 拒绝越权/注入/过期，保持幂等 |
| 台账失败、进程中断 | 停止新增写，保留进度待人工恢复 |
| 满512截断CSV、完整CSV/JSON | 只冻结完整ID |
| 待执行筛选/分页/确认/仅Video核验按钮 | 数量一致，隐藏无效核验按钮 |
| 上线 | 双静态一致、原任务34可执行、尝试与冻结ID不变 |
