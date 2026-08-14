# SA 测试用例评审

## 结论

通过。测试必须同时证明刷新成功路径、刷新失败零 X 写入、预览只读、Refresh Token 轮换和历史幂等边界。

## 覆盖性问题

| 编号 | 场景/用例 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- |
| TR-001 | TC-001/002 | 需区分授权状态与 Access Token 子状态 | 同时断言 `status`、`publish_eligible` 和四个安全字段 | 已补充 |
| TR-002 | TC-008/009 | 只测预检不足以覆盖长上传后到期 | 增加最终 source/target 写入前刷新顺序断言 | 已补充 |
| TR-003 | TC-005/006 | 永久与瞬时失败可能混淆 | 分别断言 stored status、DTO status 与 X 写入次数 | 已补充 |
| TR-004 | TC-010/011 | X Auto preview/run 副作用边界 | preview 断言 verify=0；run 断言 verify 后才冻结 snapshot | 已补充 |

## QA 修订确认

测试数据限定为临时目录和 mock；生产只做只读/离线与自然调度证据，不创建发布 canary。
