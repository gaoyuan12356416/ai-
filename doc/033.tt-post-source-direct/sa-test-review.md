# SA 测试用例评审

## 结论

通过。用例覆盖“无 FFmpeg”“输出字节等于源”“profile 隔离”“不主动发布”和回切边界。

## 覆盖性问题

| 编号 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- |
| STR-001 | 只看 mode 不能证明未制作 | 断言 runner 命令列表中不存在 FFmpeg，并比较源/输出 SHA 与大小 | 已补充 |
| STR-002 | 只测 prepare 不能证明发布仍安全 | 增加 manifest 再校验、URL Property origin 和 mock 单次 init | 已补充 |
| STR-003 | 新 profile 可能污染旧池 | 加 CPU profile 精确领取回归和生产 profile 计数只读检查 | 已补充 |
| STR-004 | 部署验证可能意外真实发布 | 以 health、日志和自然空跑验收，不手动调用 TikTok publish | 已补充 |

## QA 修订确认

测试用例已按评审意见更新。
