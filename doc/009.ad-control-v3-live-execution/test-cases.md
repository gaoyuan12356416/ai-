# 测试用例

## 测试范围

服务权限、试算 CAS、三层 pause/copy、Meta 映射、预算/ROAS、落表、幂等、额度、账号时区 scheduler、UI、V2 回归和发布回滚。

## 测试数据

- 本地：MemoryRepository、Stub Graph、临时连接和固定 UTC 时钟。
- 生产只读预检：真实表结构、Token owner、Meta GET。
- 生产写 Canary：既有测试来源，复制结果强制 PAUSED；不暂停任意 ACTIVE 业务对象。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| P0-01 | 观察/试算 | observe 规则 | preview | Meta POST=0，created_data=0 | P0 | 通过 |
| P0-02 | 手动确认 | live + 最新试算 | 错误/正确短语执行 | 错误短语零写；正确进入执行 | P0 | 通过 |
| P0-03 | 三层暂停 | 对象归属正确 | pause + GET 回读 | configured_status=PAUSED | P0 | 本地通过/生产待业务目标 |
| P0-04 | Campaign 组合复制 | 1 Campaign/N AdSet/N Ad | 执行复制 | 不调用 deep copy；逐层映射完整 | P0 | 本地通过/PAUSED Canary 待执行 |
| P0-05 | creative 废弃字段 | 来源含 standard_enhancements | 复制 Ad | 请求移除废弃字段，保留单项 features | P0 | 真实 Meta 历史证据通过 |
| P0-06 | 落表一致 | 复制 N Ad | 写事务并回读 | created_data=N，lineage=N | P0 | 本地通过/生产待执行 |
| P0-07 | 落表失败 | 注入 writer 异常 | 执行复制 | 不激活，新对象 PAUSED，intent 隔离 | P0 | 通过 |
| P0-08 | POST 超时 | 注入 timeout | 执行复制两次 | 不自动重试，同 intent 隔离 | P0 | 通过 |
| P0-09 | 暂停优先 | 同对象命中 pause/copy | 试算+执行 | 仅 pause | P0 | 通过 |
| P0-10 | 额度/冷却 | 达到日限或同来源 | 再执行 | 写前 skipped，无 Meta POST | P0 | 通过 |
| P0-11 | 账号时区 | LA 19:00 = UTC 02:00 | runner tick | 仅到期账号执行 | P0 | 通过 |
| P0-12 | 急停 | 执行多目标中急停 | 下一个目标前重读 | 不启动新目标 | P0 | 通过 |
| P1-01 | CBO/ABO | 两类来源 | 三种预算模式 | 预算写到正确层级并回读 | P1 | 通过 |
| P1-02 | ROAS 不兼容 | 非 MIN_ROAS | 请求调整 | 首次复制 POST 前阻断 | P1 | 通过 |
| P1-03 | UI | live 能力开启 | 规则列表操作 | 显示执行按钮和短语确认 | P1 | 通过 |
| P1-04 | V2 回归 | 现有代码/cron | 全量测试 | 行为无变化 | P1 | 待最终全量 |

## 回归范围

`test_ad_control_v3*.py`、全部 `test_ad_control*.py`、部署器 exact-source 校验、线上 API/UI smoke、V2 cron/服务状态。
