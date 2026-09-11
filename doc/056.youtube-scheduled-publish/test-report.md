# 测试报告

## 测试结论

2026-09-11 本地验收通过：新功能专项 50 项、浏览器 67 项均通过；扩展 Python 回归共 509 次用例执行，508 通过、1 因 Windows 不允许创建符号链接而跳过。测试包括复用/继承的基础用例，执行次数不是独立业务需求数。

隔离测试未产生生产生图、飞书、YouTube 视频、预约、取消或首评。生产上线及只读回读由主任务另行记录在 deploy.md。

## 测试范围

创建时留空/未来预约、UTC+8 转 UTC、审核/生图/处理拖过时间、原生预约成功与未知、重启意图恢复、预约修改/立即/取消、已公开竞争、临近时间拒绝、真实 SQLite CAS、首评时序、共享旧账本迁移/隔离、通知去重、Cookie/权限/同源请求以及页面日期/状态/焦点稳定。

## 执行统计

| 类型 | 数量 | 通过 | 失败 | 阻塞 |
| --- | --- | --- | --- | --- |
| Python 扩展回归（17 模块） | 507 | 506 | 0 | 1 平台能力跳过 |
| workflow + real ledger + fake platform 集成 | 2 | 2 | 0 | 0 |
| 新定时浏览器行为与桌面/移动布局 | 38 | 38 | 0 | 0 |
| 原有防闪屏浏览器回归 | 29 | 29 | 0 | 0 |

其中 Python 新功能专项引擎 33、准备/提醒 15、集成 2，共 50 项已包含在表中，不重复累加。旧共享核心/统一 writer/内部 lane 单独初测为 108/108，最终已纳入 507 项。

## 缺陷情况

- BUG-001 SQLite/进程锁顺序：已修复，同连接查询和双线程 CAS 通过。
- BUG-002 已错过预约再次审核产生新提醒键：已用固定时钟复现并修复。
- BUG-003 预约未知轮询重复提醒：状态机固定事件时刻，真实候选查询与 outbox 连续轮询验证通过。
- BUG-004 原有剧集关联 fixture 缺少 JOB_DB_PATH：只补临时路径，独立 21 项与最终全套通过。
- 另修复准备指令跨入队幂等重提、不可执行控件条件和新 HTTP 路由通用鉴权回归遗漏。无未解决功能阻断缺陷。

## 验证证据

在项目根目录运行；最终扩展回归耗时 25.483 秒：

```powershell
python -c "import sys,unittest;sys.path.insert(0,'scripts');unittest.main(module=None,argv=['unittest','scripts.test_youtube_schedule_service','scripts.test_youtube_schedule_engine','scripts.test_youtube_auto_service','scripts.test_youtube_auto_http','scripts.test_youtube_auto_engine','scripts.test_youtube_failure_notifications','scripts.test_youtube_failure_status','scripts.test_youtube_failure_diagnostics','scripts.test_youtube_failure_images_runtime','scripts.test_youtube_cover_crop','scripts.test_youtube_channel_cache','scripts.test_youtube_reference_workflow','scripts.test_youtube_reference_runtime','scripts.test_youtube_drama_association','scripts.test_drama_youtube_ads_ai','scripts.test_drama_youtube_canary','scripts.test_drama_youtube_unified_rpc'])"
python -m unittest scripts.test_youtube_schedule_integration
python -m py_compile app.py features/drama_synthesis/core.py features/youtube_auto_publish/service.py features/youtube_auto_publish/scheduling.py features/youtube_auto_publish/engine.py features/youtube_auto_publish/failure_notifications.py scripts/deploy_youtube_schedule.py scripts/test_youtube_schedule_service.py scripts/test_youtube_schedule_engine.py
node --check static/youtube-publish.js
```

部分历史 fixture 使用 bare import，聚合入口需将 scripts 加入 sys.path；初次未添加时出现的三个导入错误已由上述正确命令解决。集成脚本使用 module 入口；直接运行文件时未自动加根目录。

前端执行（本地 127.0.0.1:8898、全部 API mock，浏览器时区设为非北京时间）：

```powershell
npx --yes --package @playwright/cli playwright-cli -s=youtube-schedule --raw run-code --filename tests/youtube_publish_schedule_qa.js
npx --yes --package @playwright/cli playwright-cli -s=youtube-schedule --raw run-code --filename tests/youtube_publish_flicker_qa.js
```

结果：output/playwright/youtube-schedule-qa-result.json（38 passed、errors=[]）；桌面与移动布局由前端独立视觉核验。测试特别检查：旧预约与待确认新值并列、取消在回读前保持待确认、POST 未知不重发、日期焦点/节点不重建、桌面整行日期位置、移动无横向溢出。

## 遗留风险

- 符号链接测试因本机 OS 能力跳过；原生产故障图片 fixture 测试本轮可用并通过。
- 原有 tests/youtube_publish_browser_qa.js 的“素材配置从 false 改 true 后立刻 select-material”预期已落后于现有缓存逻辑；新代码与未修改基线 d08ae3d 都在同一选择器超时。前端保留 output/playwright/youtube-schedule-baseline-comparison.json，确认基线提供的 JS 标准化字节与该提交一致。该旧用例不纳入本次通过数；定时专项及既有防闪屏专项均通过。
- 本地 mock 不能证明 Google 实际公开的秒级延迟或取消平台回执；页面“已预约/已取消/已发布”依赖运行时真实读回，不能以本地时间到点替代回执。原生预约已受理后按 15–300 秒读回节奏确认，首评可能晚于平台实际公开。

## 发布建议

允许进入 GitHub 精确提交及部署流程；部署脚本的活动任务/未解决预约/基线漂移保护继续生效。完成服务与公开静态只读验证后，由主任务记录生产结果；不得为验收自动创建真实测试视频或发送首评。
