# 测试报告

## 当前结论

本地功能、完整相关回归、真实浏览器、代码评审、生产最小部署与线上验收均通过；需求已发布。

## 已完成证据

| 测试 | 结果 |
| --- | --- |
| 新 bridge Node 合同 | 155/155 通过 |
| 旧 bridge 回滚合同 | 53/53 通过 |
| 真实 Chrome 场景 | 30/30 通过 |
| TT Post service/core/code route | 237/237 通过 |
| 其余 TT UI/runner/app 合同 | 83/83 通过 |
| Featured/resolver/Nginx Python | 43/43 通过 |
| 生产真实 Chrome | 26/26 通过 |
| 全仓 tests 基线 | 482 项；478 通过、3 个既有失败、1 跳过 |
| `compileall` | 通过 |
| `git diff --check` | 通过 |

## 新增宏闭环验证

- 自动排期从含 `{code}` 的素材模板创建正式 queue。
- queue.code 匹配 `[A-Z0-9]{4}`，冻结 caption 使用同一个 code 且无宏残留。
- `publish_claimed` 传给 Fake GPU 的 queue.caption 与冻结值完全一致。
- GPUClient 序列化后的 HTTP payload.title 与传入 caption 完全一致。
- 立即测试含 `{code}` 返回 409，不创建 direct task，不调用 publish。

## 生产验收证据

- `/tt`、带 query 的 `/tt`、`/tt-code` 均为 200、无重定向、`no-store`/`no-cache`；三份响应 HTML SHA-256 完全相同：`033d003f79ad4c5caaa4e22c7f7c907c449f38b05345a76812ca842541072505`。
- `/tt-drama-code-search.js` 为 200，SHA-256 `4e567d580cee1ac61327399e0629a9b3f0262a698771b097f0264b48a397618f`。
- `/tt/` 与 `/tt-code/` 均为 404，证明 exact path 未扩散。
- 生产 Chrome 覆盖英语、简中、繁中、阿语、未知语言回退、桌面与 390×844；每个动态榜单固定 5 条，真实封面返回 200。
- 已发布四字符 code 搜索返回完整 8 个归因字段且 `af_channel=TT`；相同剧 ID 搜索克隆最新记录并改为 `Search`。
- Featured 点击命中无历史发布记录剧时，按确认规则使用通用 fallback 并改为 `Featured`；拖动不跳转。全部 W2A 导航在浏览器层拦截。
- v1/v2 Featured endpoints 都为 200；Nginx active，TT/资源 current 和旧静态文件哈希未改变。

## 全仓既有失败

`tests/test_ad_control_v3_routes.py` 的同一用例在 GET、POST、DELETE 三个子场景仍有既有顺序断言失败；与 032 基线一致。本次没有修改 `app.py`、ad-control 代码或相关测试，不影响本需求发布。

## 生产宏只读快照

- 当前 auto config version 7 已启用，但模板尚不含 `{code}`。
- `tt_post_recurring_pool`：31 条 available，含宏 0 条。
- `tt_post_material_intake`：63 条 ready，含宏 0 条；queued 0 条。
- 用户保存含 `{code}` 的新模板后，新入池素材会使用该宏；上述存量记录仍保留已冻结模板，未做隐式迁移。

## 发布结论

运行时提交 `29e6dd52cb1d0352a068623911197d13c77c305c` 已按单配置文件最小范围上线；无真实 TikTok 发布、无数据库写入、无静态文件覆盖。
