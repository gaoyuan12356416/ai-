# 实现计划

基线：3e6b75e，含素材 ID 搜索和明确失败后 Token 轮换。分支：codex/fb-page-excel-publish-20261021。在独立工作树实现，保留主工作区未提交修改。

## 分工与顺序

1. Excel 解析/服务：准确行号、有效/跳过结果、权限和 schema 3、复验/签名/幂等后一次冻结有效集合。
2. 来源/池/Token：批量解析及逐项错误；resolve 签名兼容现有 add；个人授权过滤与数量查询；执行/对账冻结策略和可选标题。
3. 前端：Excel 弹窗、模板下载、预览、结果；多行 ID 入池。保持 pending、版本、读取锁及迟到响应防护。
4. 兼容：批次 mixed 展示，任务行号/标题；原协议不隐式接收 schema 3，旧快照保持旧语义。
5. 测试与审查后按 deploy.md 执行 GitHub-first 上线和真实只读验收。

## 验证

新增测试按实际文件执行；既有回归包括：

```text
python scripts/test_fb_video_publish_core.py -q
python scripts/test_fb_video_page_pool.py -q
python scripts/test_fb_video_publish_gateway.py -q
python scripts/test_fb_video_publish_broker.py -q
python -m unittest scripts.test_fb_auto_repositories scripts.test_fb_auto_publisher -q
node --check static/fb-video-publish.js
git diff --check
```

新测试使用临时 SQLite、受控时钟、模拟来源/Graph。UI 使用真实源文件的隔离 fixture，不只检查语法。不得创建生产测试帖。实际命令和结果进入 test-report.md。

## 发布约束

主 API/FB sidecar 同步各自代码副本；静态资源同步发布目录和运行源并更新版本。SQL broker 白名单变化需主 API 载入；执行代码变化需安全排空共享 sidecar。不能套用此前仅 gateway 静态补丁的单服务捷径。回滚保留数据库、短链和新协议在途任务兼容能力。
