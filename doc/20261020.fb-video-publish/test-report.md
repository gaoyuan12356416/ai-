# 测试报告

## 测试结论

开发与本地验收已完成；生产部署、真实 API 与隔离 GPU 验证结果在上线后追加。
模拟 Graph 不发送真实 Facebook 帖子；UI 夹具结果与生产读回分开记录。

## 测试范围

Page 共享配置/CAS/删除重建，用户草稿及记录隔离，固定来源有界查询，多素材跨页选择，
按语言五配对，独立文案/任务/八参数短链，重发确认，原操作幂等恢复，跨入口真实 Page
占用，旧新任务竞争，GPU/链接租约与取消，未知结果，当前主 API 与导航定点集成及回滚。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| 新状态机（本地 SQLite + 模拟 Graph） | 41 | 41 | 0 | 0 |
| 来源、链接、FIFO SQL 适配器 | 29 | 29 | 0 | 0 |
| Cookie/权限/CSRF/身份映射 | 9 | 9 | 0 | 0 |
| 内部 broker/真实查询模板/集成边界 | 18 | 18 | 0 | 0 |
| 部署恢复与回滚竞态 | 7 | 7 | 0 | 0 |
| UI Playwright 断言，1440/820/390 | 71 | 71 | 0 | 0 |
| 旧自动发布针对性回归 | 94 | 94 | 0 | 0 |
| 旧素材及手动 CLI 回归 | 39 | 39 | 0 | 0 |
| 旧素材扩展回归 | 19 | 19 | 0 | 0 |
| 旧 GPU 客户端回归 | 17 | 17 | 0 | 0 |

部署恢复测试覆盖尚未迁移、复制一半、并发受理、保留历史链接和既有门禁恢复。
当前线上主 API 的真实文件变换已通过 Python 编译，当前 QuickNav 和新 JS 通过 Node 语法检查。
旧自动全量用例有一项 effects.service 用户断言与既有 root FIFO 运行约定不符，
不修改该无关服务；基线对照证据见 backend-verification.md。

## 缺陷情况

审查修复包括：短链预算预览、旧新 SQL 门禁、持久 attempt、未知请求重试误解锁、
过期异步租约回写、4 MiB 请求边界、opaque content_id、SQL 逗号/注释 JOIN 白名单绕过、
历史短链回滚保留、部分部署恢复。详见 bugs、sa-code-review、sa-test-review。

## 验证证据

代码测试驱动在 scripts/test_fb_video_publish_* 及 tests/test_fb_video_source_links.py。
本地 UI 截图和断言结果在 output/fb-video-ui（不提交生成物）；独立评审在
backend-verification.md、sa-code-review.md、source-link-contract.md。

## 遗留风险

生产 Page 授权、素材与第三方平台状态会变化，执行前仍须 fresh 校验；unknown 必须对账，
不能通过重新建批绕过。本次上线不使用真实 Facebook 发帖作为测试，Graph 上传及处理
状态转换使用模拟传输验证。计划日配额不等于实际发送日限额，积压可延后至其他日期。

## 发布建议

全部阻断项修复并通过后，执行 deploy.md 中 GitHub-first、数据盘备份和增量演练流程。
上线首次配置为空，不导入测试 Page、草稿、任务或短链。
