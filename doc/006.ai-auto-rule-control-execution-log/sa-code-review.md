# SA 代码评审

## 结论

通过。生产新规则组主路径未发现阻断上线的 P0/P1 问题。

## 评审范围

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P0 | service._decode_row | MySQL datetime不可JSON序列化 | 转标准字符串并测试 | 已关闭 |
| CR-02 | P1 | runner continuation | 跨事件累加/超限先于验收 | 分离state helper并调整顺序 | 已关闭 |
| CR-03 | P1 | execute-live | app limit只熔断单账户 | 共享Event，未发项deferred | 已关闭 |
| CR-04 | P1 | execute-live | owner字段缺失仍可写 | 缺失或不一致均跳过 | 已关闭 |
| CR-05 | P1 | persistence | 0值被`or`回退导致preview failure虚增计划数 | 按key存在性处理0 | 已关闭 |
| CR-06 | P1 | migration | upsert覆盖runner状态 | 默认跳过已有，显式force才覆盖 | 已关闭 |
| CR-07 | P2 | list API | SQLite每次解析results_json | 列表显式轻量字段，详情懒加载 | 已关闭 |
| CR-08 | P2 | deploy patch | 共享app存在漂移风险 | 线上备份、真实文件check/diff/compile | 发布门禁 |
| CR-09 | P3 | storage | 大字段长期增长 | 建议180天归档并监控 | 后续优化 |
| CR-10 | P0 | daily reducer | 同日后一event成功会掩盖前一event未完成 | 各event先取末态，再按受阻/未完成/待续跑/完成保守归并 | 已关闭 |
| CR-11 | P1 | daily key | manual带event_key会被误判scheduled | scheduled仅认runner source/actor | 已关闭 |
| CR-12 | P1 | list UI | 读取上限、分组limit和raw limit提示混淆 | 使用独立响应字段与文案 | 已关闭 |
| CR-13 | P1 | legacy status | 空status且success>0会掩盖remaining | remaining/blocked/error优先于success兜底 | 已关闭 |

## 编译 / 验证结果

- 4 个 Python 文件 `py_compile` 通过。
- `node --check static/ad-control-pages.js` 通过。
- 59 项 unittest 通过。
- 生产同源复合版 app：首次补丁 changed，二次 unchanged，`py_compile` 通过。
- 当前生产 `app.py` SHA256 与同源快照 `83d3cc8013b5e34d1e8cde4d44c4b78712d9a9c6b6cda9da7760c610f38548c1` 一致。
- 生产旧 `ad_control_rule` 数量为 0；唯一启用的是新规则组。
