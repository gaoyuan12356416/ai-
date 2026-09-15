# 部署与回滚

基线d4953ad479b2d8259dee5a9c43662918e46537a3。新脚本scripts/deploy_meta_video_delete_continue.py只部署Graph/Service两个模块；前端资源保持20260915-meta-video-account-delete并核对双份哈希。任务SQLite在线备份且不迁移。

按GitHub先行：提交后服务器精确检出、Python3.9.6回归与编译、保存原active timer状态和预览参数、等publisher自然空闲、确认无删除run/重核、主API停止后标记已确认需要重跑的预览、prepare/apply、worker-aware窄重启、finally恢复timer、原会话GET和全部旧台账对比、按原参数重新预览。

回滚使用本release的新脚本rollback BACKUP，再同样窄重启。保留当前SQLite和全部账户回执/unknown锁，不恢复备份台账。回滚到旧runner时暂停账户Video及旧节点Video两个写入口，防止操作人重新遇到已知中断问题；原始备份完整保留。恢复本版用同一备份apply，可接受该备份记录的精确guard hash。

## 已上线版本与证据

- 时间：2026-09-15 19:01（北京时间）。
- 分支：codex/meta-video-delete-continue-20260915，已推送。
- 运行提交：8798f8b098c3cd5f2e0b881c6a196399f704bff6。
- 精确检出：/mnt/data-disk/meta-ad-asset-delete/release-8798f8b098c3。
- 主机与运行路径：43.166.187.96:/root/drama_material_service，保持数据盘符号链接。
- 正式备份：/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T110150Z-8798f8b098c3。
- 预览中断前备份：/mnt/data-disk/meta-ad-asset-delete/preview-drain-20260915T110150Z-8798f8b098c3，含在线SQLite及参数。
- 主API PID 3083389→3106258，active/running；原9个active发布timer全部恢复。

服务器Python3.9.6的293项回归和编译通过。两个部署文件哈希一致；双份HTML/JS/CSS及公网哈希一致、HTTP200；topbar200，未登录产品/任务401。启动日志无Traceback/导入/语法错误。

原所有者GET两个受影响任务返回200。e5876e13ab6b4954bbdf058d5f943dbf保留1个失败、16个pending Video；cdb825c5f40f4c86b6fc66c3dffd6d93保留17个历史失败Video。与正式备份相比，222对象、84对象尝试、30回执、2账户进度、4账户尝试及全部其他旧行一致，SQLite完整性/外键检查通过。没有将旧错误直接改为成功。

本次切换时仅d2d289fb992f4372bcff7757b4ffb382仍在预览，按操作人既定选择保存参数并在旧进程退出后标记中断；另一个预览fb7cf4cf159746dd965b8c0082346327已自行结束，无需中断。通过原所有者会话单次重新提交8资源/4产品，返回202，新任务4cbb2c146ff846bb93e50a96de0171f6，预览b9b0aa189991427d8f1a183451f64d55。已核对参数/所有者完全一致，没有执行run。

备份验收文件：public-verification.json、authenticated-verification.json、ledger-verification.json、service-state-before.json、service-state-after.json、preview-resumes.json和preview-resubmission-d2d289fb992f4372bcff7757b4ffb382.json。不含Token。

## 精确回滚命令

先协调预览保存/重跑、排空执行/重核，并按service-state-before.json暂停原active的发布timer，等对应service自然空闲：

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-8798f8b098c3/scripts/deploy_meta_video_delete_continue.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T110150Z-8798f8b098c3
bash /mnt/data-disk/meta-ad-asset-delete/release-8798f8b098c3/scripts/safe_restart_drama_api.sh
```

恢复原active的9个timer：x-auto-post-runner、x-auto-post-scheduler、x-post-schedule-claim、x-post-schedule、x-post-manual、tt-post-runner、tt-auto-post-runner、tt-auto-post-scheduler、fb-auto-post-runner。验证主API与公网状态。回滚保留当前台账，旧Video写入口暂停；恢复本修复用相同备份apply并按相同排空/重启流程执行。
