# 测试报告

## 测试结论

通过。V3 FB Campaign / Ad Set / Ad 的真实暂停、复制、复制落表、隔离恢复和自动调度已部署到生产，并完成真实 Meta PAUSED Canary。新规则仍默认“禁用 + 只观察”，不会因发布自动操作业务广告。

## 测试范围

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| V3 unittest | 154 | 154 | 0 | 0 |
| V2/共享调控定向回归 | 115 | 115 | 0 | 0 |
| Python compile | 1 组 | 1 | 0 | 0 |
| JavaScript syntax | 1 | 1 | 0 | 0 |
| 生产 smoke/Canary | 1 组 | 1 | 0 | 0 |

## 缺陷情况

代码评审和生产 Canary 共发现并关闭 7 项，见 `sa-code-review.md`；无未关闭 P0/P1 缺陷。

## 验证证据

- `python -m unittest discover -s tests -p 'test_ad_control_v3*.py'`：154 tests，OK；修复后完整执行两次，结果一致。
- 旧版复制引擎、V2 部署、执行日志、runner 状态：115 tests，OK。
- 全部 `test_ad_control*.py` 共执行 297 项；294 项通过，3 项因基线分支缺少与本需求无关的 `features.x_accounts` 模块而无法导入 `app.py`，不是本次代码失败，生产部署前以线上完整运行目录补做 smoke。
- `node --check features/ad_control_v3/assets/app.js`：通过。
- 2026-07-16 真实 Meta 证据：Graph v25.0 组合浅复制 Campaign/Ad Set/Ad 成功且均 PAUSED；直接 deep copy 报 1885194；清理废弃 creative 字段后 Ad copy 成功。
- 2026-07-17 生产真实复制 Canary：来源 Ad `120245745070090068`，同一 intent `6a19ac49edf24c4c8363ef1a7aa76362` 生成 Campaign `120245766848190068`、Ad Set `120245766849030068`、Ad `120245766850070068`、Creative `1038438105346387`，最终全部 PAUSED。
- 复制预算由来源 Campaign 的 50% 计算为 `9750` cents；`ads_ai.ads_facebook_auto_created_data` 新增 1 行，created_data ID `1`；lineage 新增 1 行且来源字段、素材、产品和剧目信息一致。
- 首次落表遇到 Meta ISO 时区时间与 MySQL DATETIME 不兼容，系统按设计保持新 Meta 对象 PAUSED、目标表零写；修复后用原 intent 补齐落表，恢复过程 `meta_write_count=0`，再次重放返回 `duplicate_completed_intent`，没有重复复制。
- 真实暂停 Canary：在父 Campaign / Ad Set 均保持 PAUSED 的前提下临时将新 Ad 配置为 ACTIVE，再由执行器写回 PAUSED；`meta_write_count=1`，来源对象状态未改变。
- 生产 FB 镜像表与源表均为 56 列、49 个索引签名项，schema hash 均为 `cb6f3841f5afe43b5984e01a1d9322b945f7961992281ff9f8bd6a7564a01329`。
- 最终线上代码 commit `3a70e8346f5e77e47af3bb3cd943855386304460`；API active，V3 timer active/enabled，11:14:30 后应用错误数为 0，连续 runner tick 成功。
- 已登录线上 UI 回读确认：`正式执行可用`，暂停、复制、自动调度能力均为 true；新规则启用和正式执行仍需要显式确认。

## 遗留风险

尚未用真实业务对象执行 ACTIVE 放量。复制链路已验证到“创建 PAUSED → 落表/校验 → 激活开关可用”，今天首次业务测试应选择明确的测试对象并限制为单次；发生异常立即关闭复制熔断并依据 lineage 精确 PAUSE。

## 发布建议

允许开始受控业务验收。先试算，再手动执行一条已确认的正式规则；核对执行日志和 Meta 实际状态后，才启用该规则的定时运行。操作步骤见 `live-test-guide.md`。
