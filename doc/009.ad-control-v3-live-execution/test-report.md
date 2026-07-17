# 测试报告

## 测试结论

本地 V3 阶段通过；生产部署/Canary 尚未执行，最终结论待补。

## 测试范围

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| V3 unittest | 151 | 151 | 0 | 0 |
| V2/共享调控定向回归 | 115 | 115 | 0 | 0 |
| Python compile | 1 组 | 1 | 0 | 0 |
| JavaScript syntax | 1 | 1 | 0 | 0 |
| 生产 smoke/Canary | 待执行 | 0 | 0 | 待部署 |

## 缺陷情况

代码评审发现并关闭 5 项，见 `sa-code-review.md`；无未关闭 P0/P1 本地缺陷。

## 验证证据

- `python -m unittest discover -s tests -p 'test_ad_control_v3*.py'`：151 tests，OK。
- 旧版复制引擎、V2 部署、执行日志、runner 状态：115 tests，OK。
- 全部 `test_ad_control*.py` 共执行 297 项；294 项通过，3 项因基线分支缺少与本需求无关的 `features.x_accounts` 模块而无法导入 `app.py`，不是本次代码失败，生产部署前以线上完整运行目录补做 smoke。
- `node --check features/ad_control_v3/assets/app.js`：通过。
- 2026-07-16 真实 Meta 证据：Graph v25.0 组合浅复制 Campaign/Ad Set/Ad 成功且均 PAUSED；直接 deep copy 报 1885194；清理废弃 creative 字段后 Ad copy 成功。

## 遗留风险

生产 DDL、Token 读取、PAUSED Canary、systemd timer 和 ACTIVE 首次业务执行尚需在线验证。

## 发布建议

允许进入“备份 → GitHub 精确发布 → DDL → PAUSED Canary”；PAUSED Canary 未通过前不得开放自动激活。
