# SA 测试用例评审

## 结论

通过。测试已覆盖批次公平性、Graph限流熔断、owner fail-closed、续跑关键状态、MySQL序列化、UI懒加载和部署补丁幂等；线上项在部署阶段执行。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-01 | code4 | 只测错误分类不足以证明熔断 | 执行注入版 live function，断言Graph调用次数与deferred | 已关闭 |
| TR-02 | runner | 需覆盖跨事件/跨午夜/零目标上限顺序 | 增加纯状态函数测试及结构顺序断言 | 已关闭 |
| TR-03 | MySQL API | DATETIME序列化风险 | 增加真实datetime JSON测试 | 已关闭 |
| TR-04 | migration | 二次运行可能覆盖状态 | 默认跳过已存在，线上执行两次核验 | 待线上验证 |
| TR-05 | 生产 | 禁止首测真实暂停 | 先建表/回填/GET API，再 preview/dry-run | 已纳入部署步骤 |
| TR-06 | 日聚合 | 需证明最终完成覆盖历史partial | 增加多批+最终executed状态reducer测试 | 已纳入TC-22/23 |
| TR-07 | 计数 | 直接SUM会重复目标 | 断言列表文案为执行尝试，并保留逐批证据 | 已纳入TC-28 |
| TR-08 | 日期/分页 | 跨午夜与窗口截断可能拆链 | 增加event_key业务日和最老组丢弃测试 | 已纳入TC-26/29/30 |
| TR-09 | 兼容 | daily改动不能破坏raw/targets | 增加view=raw和逐批lazy测试 | 已纳入TC-31 |
| TR-10 | 多事件 | 同日新事件成功可能覆盖旧事件失败 | 增加blocked/partial事件后接executed的负向测试 | 已关闭 |
| TR-11 | 历史兼容 | 空run_status但有remaining可能被success兜底误报 | remaining优先并增加version1负向测试 | 已关闭 |
| TR-12 | 真实查询 | 跨午夜纯reducer测试不能证明SQL窗口正确 | daily前后扩日、stub执行日期过滤并做API函数测试 | 已关闭 |

## QA 修订确认

本地自动化测试已扩充到59项；线上只读/无副作用验证列入 TC-14、TC-19、TC-21。
