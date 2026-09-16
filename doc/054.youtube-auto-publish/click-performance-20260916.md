# 详情点击、封面和轮询性能优化

用户点击“查看详情 / 审核封面”后立即显示可关闭的加载弹窗，异步读取当前任务；读取失败留在弹窗内重试。完整详情返回前不展示审核动作，返回后使用最新版本，服务端权限、版本 CAS、频道 fresh-check 和发布流程保持原约束。关闭会中断初次读取，迟到响应不会重新打开弹窗。

列表使用独立的 160×90 JPEG 缩略图（等比缩放，质量 75），大封面仅在详情需要时加载。缩略图按原始 SHA 保存在进程内 LRU，最多 128 项、最多两个并行解码；不新增磁盘缓存或修改任何原图。封面及缩略图都必须先校验当前 Cookie、模块权限、租户、owner / admin 权限及原文件 SHA。

图片返回 `Cache-Control: private, no-cache, max-age=0, must-revalidate`、`Vary: Cookie` 和各自的 ETag。这里的 no-cache 允许浏览器存储，但每次复用须重新鉴权；匹配 ETag 后返回无正文的 304。匿名、无权限、其他 owner / tenant 和已变化的原文件仍返回原有拒绝结果，不能因 ETag 命中跳过校验。Nginx 关闭此路由的共享代理缓存，不再添加重复 no-store 头，由应用决定每种响应的缓存规则。

`GET /tasks?compact=1` 只投影列表需要的字段，省略文案、宏链接、历史版本、阶段详情和通知记录，也跳过通知投影查询。默认未指定 compact 时保持完整 DTO，兼容旧页面。摘要响应包含按数据和筛选条件计算的 revision，后续相同查询附带 since；内容未变化只返回 `{unchanged:true,revision:...}`。JSON 仍为 no-store。Nginx 对 JSON 启用 gzip。详情 / 审核弹窗打开期间不下载后台列表，继续每 6 秒刷新当前详情；关闭后更新列表。

## 验证

- 本地 Python 相关模块全套 441 项，440 通过，1 个既有图片 fixture 跳过。
- 新增摘要大小、跳过通知、变更 revision、搜索隔离、owner / tenant 隔离、条件请求前权限检查、源图片完整性、160×90 尺寸、原图不变等断言。
- 本地接口替身专项 12 项通过：立即挂载弹窗、等待时可关闭、迟到响应不重开、失败重试、列表使用缩略图、未变更轮询、弹窗内暂停列表流量、审核使用最新版本，以及移动布局。一次实测点击至下一帧约 6.3 ms，仅代表本地 UI，不代表生产网络端到端耗时。
- 原防闪屏 29 项、预约发布 38 项浏览器回归通过；连同新专项共 79 项断言。保持原 DOM、滚动、视频节点与预约 CAS 行为。
- 图片和列表的生产传输量以部署后的真实鉴权 GET 测量为准。

## 部署和回滚

分支 `codex/youtube-click-performance-20260916`。先提交并推送 GitHub，再从服务器仓库 fetch 精确提交，归档至 `/mnt/data-disk/deploy/youtube-auto-publish/releases/<SHA>` 并标记 `.github-verified-commit`。

部署脚本 `scripts/deploy_youtube_click_performance.py --commit <SHA> --check` 先检查当前生产 SHA、数据盘 UUID 和服务状态。去掉 `--check` 后备份 8 个目标的原始字节、权限和 SHA；新的 covers.py 记录为原本不存在。只替换生产 app.py 的 YouTube 路由方法，保留其余生产修改。其余变更为 service.py、covers.py、两份 HTML / JS 及该模块 Nginx 配置。只重启 API、平滑重载 Nginx，发布 worker 和统一 writer 的状态必须不变。任何失败触发代码回滚。

回滚命令：

```bash
python3 /mnt/data-disk/deploy/youtube-auto-publish/releases/<SHA>/scripts/deploy_youtube_click_performance.py --rollback <BACKUP> --check
# 核对通过后，删除 --check 执行回滚。
```

回滚拒绝覆盖后续漂移；只恢复代码和配置并移除这次新增的 covers.py，不恢复数据库、不删除原图、不修改已有发布或预约。
