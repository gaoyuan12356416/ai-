# 测试报告

## 测试结论

通过本地回归，具备受控部署条件。真实 TikTok 初始化由用户在部署后亲自点击，本报告不包含真实发布结果。

## 测试范围

- TT Post core/store。
- CPU sidecar、账号/素材/排期/队列/runner。
- GPU prepare/publish/reconcile、credential envelope、ledger。
- 管理页静态交互契约。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python 单元/契约测试 | 220 | 220 | 0 | 0 |
| Python 语法编译 | 4 个模块 | 4 | 0 | 0 |
| 真实 TikTok canary | 1 | 待用户点击 | 0 | 0 |

## 关键验证证据

- 正式门禁全部关闭时，普通人工发布继续失败且不消费素材。
- 正式门禁全部关闭时，精确 canary 生成 SELF_ONLY 队列，关闭评论/Duet/Stitch 和商业开关。
- 自动 due 不能使用 canary；已有每日排期启用时页面也不会显示 canary 可发布。
- 错账号、素材、Job 或成片指纹在 TikTok 调用前失败。
- canary 使用独立 GPU 路由和 `canary_publish` credential operation。
- 旧 manifest `direct_post_eligible=false` 不被修改；只对精确 canary 运行例外。
- GPU 在 init 前写账本；第二次调用不会再次 init。
- TikTok 400 的 HTTP status、code、message、log ID 被安全保留。
- 过期但格式正确的许可不会阻止 CPU/GPU 服务启动，只会变为 inactive。

## 缺陷情况

开发中发现并修复 3 项：credential operation 白名单遗漏、素材预制作变量误用、过期许可启动行为。

## 遗留风险

- TikTok 可能因客户端审核、URL Property、内容/品牌引导等原因拒绝；这正是本次真实 canary 需要采集的结果。
- 若取得 `publish_id`，只能核对状态；若结果未知，不允许自动重试。
- 测试许可到期后按钮会自动禁用。

## 发布建议

先部署 GPU，再部署 CPU；两端设置相同的短时目标许可，保持三重正式门禁为 0。部署后只读确认页面按钮与队列基线，由用户亲自点击。
