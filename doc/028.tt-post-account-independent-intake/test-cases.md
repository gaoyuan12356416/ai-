# 测试用例

| 编号 | 场景 | 预期结果 | 优先级 |
| --- | --- | --- | --- |
| T01 | 启用配置，Creator Info 故障 | 配置保存成功且未调用 Creator Info | P0 |
| T02 | 启用配置含不可信账号 | 原子拒绝，配置和 schedule 均不写入 | P0 |
| T03 | 入池不传账号、配置版本有效 | 服务端稳定分配到已保存账号 | P0 |
| T04 | 同素材同幂等键重试 | 返回同一 intake 和同一账号 | P0 |
| T05 | 入池版本过期或账号列表为空 | 拒绝且不写入 | P0 |
| T06 | 旧调用显式传配置成员账号 | 继续成功 | P1 |
| T07 | 旧调用显式传非成员账号 | 拒绝 | P1 |
| T08 | 预制作时账号源/Creator Info 故障 | 制作仍完成，不调用两者 | P0 |
| T09 | 预制作成片时长非正数 | 制作失败并记录错误 | P0 |
| T10 | 最终自动发布 | 仍调用 Creator Info 并检查账号最大时长 | P0 |
| T11 | 页面未选立即测试账号 | 配置已保存且素材校验通过即可入池 | P0 |
| T12 | 页面立即测试 | 仍要求选账号、设置和 Creator Info | P0 |
| T13 | 入池 payload | 不发送 `source_account_id`，发送配置版本 | P0 |
| T14 | 全量 TT Post 回归 | 所有脚本通过，无既有合同回归 | P0 |

## 测试数据

全部自动化使用临时 SQLite、fake 账号源、fake 素材解析和 fake GPU；不连接生产 TikTok。

## 回归范围

账号设置、GPU worker、链接、页面、prepare runner、app contract、core、service、Node bridge。
