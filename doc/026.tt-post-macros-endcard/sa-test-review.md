# SA 测试用例评审

## 结论

测试设计**有条件通过**。`test-cases.md` 已覆盖宏、冻结、短链、媒体、UI 和回滚六个维度共 42 条用例，并明确了 prepare-only 禁令。

当前只是用例覆盖性评审，42 条均未执行；在真实证据回填前，发布门禁保持阻塞。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | M05 非递归 | 只测模板有多个宏不足以证明 desc 内 token 不会二次解析 | 用 MAT-B 同时放 `{url}`、`{desc}`、`{{content_id}}` 字面值并精确断言 | 已补充 |
| TR-002 | M07/M08 UTF-16 | 普通 ASCII 边界无法发现 emoji 计数差异 | 正/负边界都至少含一个补充平面 emoji | 已补充 |
| TR-003 | F05 冻结 | 只比较 intake/pool 值不足以证明 queue/replay 不查源库 | 建 queue 后修改测试源库，再重读和幂等重放 | 已补充 |
| TR-004 | F07 历史迁移 | 默认空字符串可能掩盖老可用记录风险 | 同时覆盖已发布不回写与 available+`{desc}` 被阻断 | 已补充 |
| TR-005 | U05/U06 路由 | 单测无法证明 Nginx 优先级，也可能伤及 X 链接 | 隔离 Nginx 中成对验证 TT/X URL，并做 no-cache 请求 | 已补充 |
| TR-006 | D01/D03 片尾身份 | 仅看成片时长不能证明使用审核资产 | 检查 outro/logo SHA/size、manifest、抽帧和人工观看 | 已补充 |
| TR-007 | D05/D06 旧模式 | 新 eligibility 判断可能让 direct_clean 误加片尾或 preview 可直发 | 锁定命令数量/filter/profile/health 的旧合同 | 已补充 |
| TR-008 | D08/S01 外部副作用 | “没有点发布按钮”不能证明服务没发请求 | 比对 HTTP audit、publish ledger、queue、Post 和 schedule 基线 | 已补充 |
| TR-009 | I04 排期关闭 | 有效账号 happy path 不能覆盖用户反馈的“无法关闭” | 用无效/管理占位账号，断言关闭不依赖时间/consent/creator-info | 已补充 |
| TR-010 | S03/S04 回滚 | 只写回滚命令没有证明数据和旧模式可用 | 在隔离副本做 CPU/GPU 两条独立回滚演练 | 已补充 |

## QA 修订确认

- 已把所有用例状态设为“待执行”，未伪造通过结果。
- 已加入禁止调用 publish/canary/run-now/schedule-save 的明确前置。
- 已加入 2200/2201 UTF-16、description 冻结漂移、片尾资产指纹和 prepared/source URL 身份。
- 已加入 2c 自动排期关闭、管理占位、dirty draft、状态计数与 run-now gate 回归。
- 执行后必须在 `test-report.md` 回填命令、环境、时间、日志/manifest/抽帧位置与失败详情。
