# SA 测试用例评审

## 结论

**待独立 SA/QA 评审。** 实现自测不替代 QA gate。

## 必查覆盖

| 编号 | 场景 | 状态 |
| --- | --- | --- |
| SA-T01 | 404 expired session 与未知关闭 | 待补充/评审 |
| SA-T02 | worker crash 和过期 lease 重领 | 待补充/评审 |
| SA-T03 | 浏览器现有视觉/侧栏/表单/卡片/表格回归 | 待浏览器评审 |
| SA-T04 | HK 真机 FFmpeg profile 和资产清单（不部署） | 待受控环境评审 |
| SA-T05 | API 不泄漏 credentials/session URI | 待安全评审 |
| SA-T06 | 不触发真实 YouTube post/comment | 必须保持 |

## QA 修订确认

待独立 QA 填写用例编号、证据与结论。
