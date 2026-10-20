# 部署文档

## 基线与范围

CPU `43.166.187.96`。FB运行 `41b5ca4f1e8f64945f56b2482e8e3dd96e2c9e89`；
`/opt/fb-auto-post/current` 和主API `/root/drama_material_service` 均非Git工作树。
部署工具只从已推送精确提交读取文件，通过当前在线 app/nav 的SHA256和锚点定点合入；
不复制该历史分支的 app.py、不部署 `features/fb_gpu` 或 `features/random_gpu`。

新增完整 `features/fb_video_publish` 分别装入主API/FB sidecar；旧FB只白名单
core/gpu(客户端)/hit_material/manual_batch/pool_extension/service。新页面装入主API、
FB运行目录和Nginx静态目录。当前共享QuickNav保留所有既有入口和折叠行为。

## 配置

FB sidecar drop-in：`/etc/systemd/system/fb-auto-post-service.service.d/fb-video-publish.conf`。
复用既有内部 bearer 作预检签名，不增加客户端可见秘密。

| 配置 | 生产值 |
| --- | --- |
| FB_VIDEO_PUBLISH_ENABLED | 1 |
| FB_VIDEO_DAILY_LIMIT | 1000 |
| FB_VIDEO_SHARED_GPU_PREPARES | 2，保持原CPU准备worker并发上限，不更改GPU容量 |
| FB_VIDEO_SHORT_LINK_ROOT | /mnt/data-disk/fb-auto-post-public/s2l/facebook |
| FB_VIDEO_SHORT_LINK_OWNER | fb-auto-post |

新短链目录 0750、文件0640，属主/组fb-auto-post；仅该独立目录继承Nginx只读ACL。
公网路由 `/s2l/facebook/` 精确限制正整数html，no-store，不覆盖旧fb/youtube。
新worker `fb-video-publish-worker.service`，准备 `fb-video-publish-prepare.timer/service`。

## 数据与上线顺序

所有新增配置/草稿/任务/账本均为现有 operational SQLite 的 `fb_video_*` 增量表。
跨入口trigger保护使用同库的未升级旧CLI。不向业务MySQL写入，不导入COS测试数据。

1. 通过本地回归和SA评审，commit/push GitHub。
2. CPU数据盘UUID必须为 `3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`、可写且空间充足。
3. CPU从GitHub检出精确提交；运行 `python3 scripts/deploy_fb_video_publish.py` 预检：
   当前文件hash、在线SQLite backup至隔离副本、迁移演练、旧业务事实hash和integrity_check。
4. `--apply` 记录原8个FB timers状态，暂时停止tick并要求在途准备/上传已结束；未结束即退出并恢复timer。
5. 数据盘备份原文件、在线DB、服务状态及manifest；按清单原子替换、增量迁移；
   `nginx -t`后reload，仅重启主API/FB sidecar并启用新worker。恢复原timer活跃状态。
6. 验证health、准确文件SHA、401/403、非空真实DTO、入口及三个分辨率。
   模拟Graph验收；真实GPU只做独立prepare-only，不创建正式发布任务/短链或测试帖。

## 回滚

使用当前GitHub检出的部署脚本：

```bash
python3 scripts/deploy_fb_video_publish.py --rollback /mnt/data-disk/fb-video-publish/backups/<实际部署目录>/manifest.json
```

先关闭新worker和准备timer。存在新活动/unknown任务时关闭新入口和新提交，
保留跨入口门禁及源代码，提示人工对账，不能强行撤掉保护。没有新活动任务才恢复对应代码。
数据库永不恢复旧备份；保留新记录、发布事实、编号和短链文件。
**只要已有任一正式短链账本，回滚必须保留 `/s2l/facebook/` 的Nginx路由，
以保证历史帖文链接仍可访问。** 回滚前文件发生新变化时拒绝覆盖。

## 已部署版本（2026-10-20）

代码：`f5fcb5b711656f785434dd2494d608ed01d5d757`，分支 `codex/fb-video-publish-20261020`。
release：`/mnt/data-disk/fb-video-publish/releases/f5fcb5b`。
主 API/FB sidecar健康，新worker已启用，旧8个timers状态保持，60个文件hash均一致。
GPU prepare-only和真实Cookie/非空素材/Page验收详见test-report。

正式入口：

- https://ai.yingliangads.com/fb-pages.html
- https://ai.yingliangads.com/fb-video-publish.html
- https://ai.yingliangads.com/fb-video-publish-records.html

本次准确回滚命令：

```bash
cd /mnt/data-disk/fb-video-publish/releases/f5fcb5b
python3 scripts/deploy_fb_video_publish.py --rollback /mnt/data-disk/fb-video-publish/backups/20261020T055525Z-f5fcb5b7/manifest.json
```

命令自动按新任务事实选择关闭入口并保留守卫，或完整恢复对应代码；不恢复旧数据库。
若部署目标后来发生其他修改，先审查漂移再回滚，禁止绕过hash检查。
