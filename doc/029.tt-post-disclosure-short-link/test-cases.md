# 测试用例

| 编号 | 场景 | 预期结果 | 优先级 |
| --- | --- | --- | --- |
| T01 | 账号设置自有品牌=true，新建自动 queue | queue 双披露均 false | P0 |
| T02 | 账号设置第三方品牌=true，新建自动 queue | queue 双披露均 false | P0 |
| T03 | 旧 claimed queue 披露=true，begin publish | 原子归零并返回 false 快照 | P0 |
| T04 | GPU publish 自动路径 | `post_info` 双披露均 false | P0 |
| T05 | direct-test 披露=true | 仍按独立冻结值，不受本需求影响 | P0 |
| T06 | 新 queue ID=6 且有 `{url}` | URL 为 `/s2l/tt/6.html` | P0 |
| T07 | 新短链文件 | 写入 `s2l/tt/6.html`，内容不可变 | P0 |
| T08 | 并发创建 queue | 每条 link ID 等于自身 queue ID，无重复 | P0 |
| T09 | exact replay 新短链 | 返回原记录，文件目标一致 | P0 |
| T10 | 历史 19 位 queue replay/prepare | 继续成功，不转换 URL | P0 |
| T11 | X `/s2l/6.html` | hash/内容不变，新 TT 路由不抢占 | P0 |
| T12 | Nginx `/s2l/tt/数字.html` | GET 200；非法路径 404；POST 拒绝 | P1 |
| T13 | 无 `{url}` 模板 | short fields 仍为 0/空 | P1 |
| T14 | 全量 TT 回归 | 无发布、队列、direct-test 回归 | P0 |

## 测试数据

自动化使用临时 SQLite、fake 账号/素材/GPU；生产只读核对历史 TT/X 文件和任务快照。
