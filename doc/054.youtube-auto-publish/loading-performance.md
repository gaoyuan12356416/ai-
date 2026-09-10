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

## 生产执行记录

2026-09-10 17:07，提交 `e958789ba57617552c95f8d9799ec62317af4997` 已推送GitHub，CPU `43.166.187.96` fetch后核对FETCH_HEAD一致，再从GitHub archive部署到 `/root/drama_material_service` 和 `/usr/share/nginx/html`。源和公共静态共8个文件摘要与发布目录完全一致。生产Python3.9重新运行service 43项、HTTP 17项，全部通过。

备份：`/mnt/data-disk/deploy/youtube-auto-publish/backups/loading-20260910-170743-e958789ba576`，含manifest、原文件摘要及result。仅重启主API和依赖它的旧YouTube worker；新worker与统一writer未重启，四服务active、NRestarts=0。API健康检查和公共功能guard通过。

公网HTML、JS、CSS均200、SHA256与GitHub发布字节一致，HTML引用`v=20260910-loading-v1`；轻量bootstrap和新channels匿名均401。使用真实生产适配器、独立只读actor进行函数计时：bootstrap_lite 1.50 ms、任务列表1.19 ms、频道3398.23 ms（25个）、素材2106.61 ms（3条）。该计时不含浏览器、网络和真实Cookie鉴权；未取得用户登录浏览器的整页耗时，不能宣称整页1.5 ms。

SQL摘要仍为`c301d02c6bfad8fa62b86cc2befe83176a38616c4d42c9e385bfb78e0e838e01`。准备/资产/通知仍零条；既有账本published/published2、published/skipped1、unknown/queued2保持不变。所有测试均未触发真实发布、生图、评论或飞书消息。

本次准确回滚命令（保留当前SQL和数据库）：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/e958789ba57617552c95f8d9799ec62317af4997/scripts/deploy_youtube_loading_fix.py --rollback /mnt/data-disk/deploy/youtube-auto-publish/backups/loading-20260910-170743-e958789ba576
```
