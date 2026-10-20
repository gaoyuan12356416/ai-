# 部署与功能回退

2026-10-20 17:52（北京时间）部署完成。

- 实现提交：`2e28d39f93768b3388c4a037fd8ed8b206948168`，分支`codex/fb-page-material-pool-20261020`，已推送GitHub并在CPU检出验证。
- 服务器：`43.166.187.96`。
- GitHub检出目录：`/mnt/data-disk/fb-video-publish/releases/2e28d39`。
- FB运行目录：`/opt/fb-auto-post/current`；主API：`/root/drama_material_service`；静态：`/usr/share/nginx/html`。
- 本次只同步37个文件，主API使用定点patch（添加4行私有身份路由），未整包覆盖主API/导航，未部署GPU源码。
- 备份manifest：`/mnt/data-disk/fb-video-publish/backups/pool-20261020T095200Z-2e28d39f/manifest.json`。同目录包含逐文件备份和SQLite在线备份`operational.sqlite3`。
- 开关文件：`/etc/systemd/system/fb-auto-post-service.service.d/zz-fb-video-pool.conf`，`FB_VIDEO_V2_ENABLED=1`和`FB_VIDEO_POOL_ENABLED=1`；原live/prepare开关继续生效。

## 执行与验证

GitHub-first后先运行`python3 scripts/deploy_fb_page_material_pool.py`演练，再用`--apply`部署。确认数据盘UUID、基线hash、源/目标hash；暂停生产者、等待活跃请求结束、备份在线SQLite并执行增量迁移。

副本及正式库迁移均校验25张旧表、136085行的所有旧列摘要一致，SQLite integrity_check通过。迁移新列及pool表不改旧内容。37个部署目标SHA全部匹配；5份公网HTML/JS/CSS为HTTP200且字节与磁盘一致。

主API和FB sidecar窄重启后健康检查通过，原发布worker已恢复，新增`fb-video-pool-scheduler.service`已enable/active。原8个自动timer及原手动prepare timer的active/enabled状态保持。新scheduler验收时无错误日志、NRestarts=0。

真实登录页面读取3个Page及历史创建人姓名成功。素材池GET返回v0/paused/空列表，真实英语素材库可分页多选；视频发布真实选择2素材后显示两个独立可清空预约框，验证一空一预约。该浏览器验收没有保存草稿、入池、启用计划或提交发布。

内部身份broker实际HTTP200并匹配原发起人、有效归因；公网同私有路径HTTP404。生产仍只有原1条published任务、1份短链；pool/slot行数均0。短链文件集合摘要前后均为`7053156df540516e23183e2f59517f9bc0157ba44ef1c0176f5cdb307d5dbf04`。

## 准确回退命令

在CPU服务器执行：

```bash
python3 /mnt/data-disk/fb-video-publish/releases/2e28d39/scripts/deploy_fb_page_material_pool.py --rollback /mnt/data-disk/fb-video-publish/backups/pool-20261020T095200Z-2e28d39f/manifest.json
```

脚本先校验兼容backend和静态备份SHA，再排空活动请求。关闭新增scheduler并将V2/POOL开关置0，恢复上版静态；保留新backend、数据库及所有任务/短链，继续执行已经冻结的任务。原自动timer和手动worker按执行前状态恢复。**不使用旧版全量回滚脚本恢复旧core，不用备份数据库覆盖现库。**

若后续代码hash改变或部署曾在迁移/复制中途失败，脚本拒绝盲目回退；按manifest先恢复完整兼容的任务执行代码。不能为了恢复UI而中断平台结果不明的发布，不能重用链接编号。

## 运行边界

新scheduler每10秒轮询，单轮最多20个候选，30秒后不发起新来源检查；事务提交前仍核验60秒时点有效期，慢来源导致跳过且不补发，不提高SQL/GPU并发。当前3个Page已完成真实查询验证；大量Page同一分钟触发需要另做容量评估。
