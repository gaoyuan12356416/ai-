# 测试报告

## 当前结论

本地功能、完整相关回归、真实浏览器和代码评审均通过；待生产最小部署与线上验收后给出最终发布结论。

## 已完成证据

| 测试 | 结果 |
| --- | --- |
| 新 bridge Node 合同 | 155/155 通过 |
| 旧 bridge 回滚合同 | 53/53 通过 |
| 真实 Chrome 场景 | 30/30 通过 |
| TT Post service/core/code route | 237/237 通过 |
| 其余 TT UI/runner/app 合同 | 83/83 通过 |
| Featured/resolver/Nginx Python | 43/43 通过 |
| 全仓 tests 基线 | 482 项；478 通过、3 个既有失败、1 跳过 |
| `compileall` | 通过 |
| `git diff --check` | 通过 |

## 新增宏闭环验证

- 自动排期从含 `{code}` 的素材模板创建正式 queue。
- queue.code 匹配 `[A-Z0-9]{4}`，冻结 caption 使用同一个 code 且无宏残留。
- `publish_claimed` 传给 Fake GPU 的 queue.caption 与冻结值完全一致。
- GPUClient 序列化后的 HTTP payload.title 与传入 caption 完全一致。
- 立即测试含 `{code}` 返回 409，不创建 direct task，不调用 publish。

## 待完成

- GitHub 精确提交部署后的 Nginx、HTTP、浏览器、服务 current 与自然定时器验收。

## 全仓既有失败

`tests/test_ad_control_v3_routes.py` 的同一用例在 GET、POST、DELETE 三个子场景仍有既有顺序断言失败；与 032 基线一致。本次没有修改 `app.py`、ad-control 代码或相关测试，不影响本需求发布。

## 生产宏只读快照

- 当前 auto config version 7 已启用，但模板尚不含 `{code}`。
- `tt_post_recurring_pool`：31 条 available，含宏 0 条。
- `tt_post_material_intake`：63 条 ready，含宏 0 条；queued 0 条。
- 用户保存含 `{code}` 的新模板后，新入池素材会使用该宏；上述存量记录仍保留已冻结模板，未做隐式迁移。
