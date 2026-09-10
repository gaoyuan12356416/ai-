# 工作台加载修复（2026-09-10）

问题与修复设计见 [BUG-005](bugs/BUG-005.md)，新增接口见 [api-doc.md](api-doc.md)。

## 变更范围

只更新CPU主API的app.py路由、YouTube service.py的初始化/频道查询，以及youtube-publish.html/js/css的两份静态文件。导航配置、SQL、环境变量、数据库结构、HK执行器、发布状态机均不变。

GET请求最多等待12秒，POST最多45秒，均包含正文读取时间。频道请求从首屏移到新建弹窗，缓存60秒；发布提交仍实时校验频道资格。导航配置请求不会阻塞任务工作台。任务列表和频道各自显示加载/失败状态，可重试。

## 部署与回滚

使用 `scripts/deploy_youtube_loading_fix.py --commit <已推送并由服务器fetch验证的40位SHA>`，仅可从带`.github-verified-commit`标记的GitHub发布目录执行。脚本检查当前部署摘要、SQL摘要、数据盘、YouTube无活动上传/评论和公共功能保留规则；备份8个源/静态文件，然后原子替换。

只重启 `drama-material-api.service` 及其依赖的 `drama-youtube-publish-worker.service`。新封面worker和统一writer保持运行。检查四服务active和`/api/auth/status`健康；切换失败自动恢复本次文件备份，不恢复数据库。

手动回滚命令：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/<本次40位SHA>/scripts/deploy_youtube_loading_fix.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/<本次loading备份目录>
```

## 本地验证

后端service 43项、HTTP 17项通过；浏览器67项通过（原34、慢请求23、并发10），没有JavaScript异常。并发用例确认：详情先于空列表返回仍可审核、旧详情轮询晚返不会覆盖POST结果、审核成功立即解除忙碌、自动刷新失败提示保留。Python编译、Node语法、diff检查、公共功能guard 5功能/22文件规则通过。

受控mock网络测量：导航永久悬挂时工作台247 ms可见，新建弹窗157 ms。频道超时12.35秒后可重试，POST45.44秒后保留草稿和同一operation_id，不自动重发。此为浏览器模拟慢请求验证，不等同于生产整页耗时。

1440×1000截图（本地测试数据）：

- `C:/Users/gaoyu/Documents/New project/output/playwright/youtube-production-frontend/loading-fixed-workspace-1440.png`
- `C:/Users/gaoyu/Documents/New project/output/playwright/youtube-production-frontend/loading-fixed-channel-pending-1440.png`

具体版本、备份路径及生产测量在部署后追加。所有测试均不触发真实发布、生图、评论或飞书消息。
