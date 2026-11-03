# 部署与回滚

CPU：43.166.187.96；真实数据盘UUID 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8。GPU节点43.154.250.89未重启或换代码。

## GitHub版本与线上路径

- 分支 `codex/pipeline-unblock-20261103`，GitHub gaoyuan12356416/ai-。
- 静态 `eaff575f45e023d9a71cd198c87220027c4ca470`：/root/drama_material_service/static 与 /usr/share/nginx/html 的 pipeline-monitor.js/css/html。
- 精确恢复工具 `7439c7af5f4657f1003fa44e952f8b38acedf1ea`；FB Graph Python3.9兼容 `d119792b9730900e6569cfe3e904709cdc46377c`。
- TT readback补丁 `5caa22acc0d9bf0cf251a33dbf37ffde1c292235`，精确部署脚本版本 `e4b63df7a8aa3b239b1da44c709bc423a1db7e8d`。只改 /opt/tt-auto-post/current/features/pipeline_dispatch/tt_readback.py，新SHA256 71781bd3e4f4c7cc8d756de604dc8679003962e517e6f611564457f2c17db8e3。
- 实际TT运行根 /mnt/data-disk/pipeline-dispatch-runtime/backlog-native-d69a369660/candidates/tt_auto；其余运行文件完整保留，不用本分支全量覆盖生产。

## 备份和审计

- 静态 /mnt/data-disk/pipeline-dispatch/monitor-static-20261103T105653Z，deploy.json有精确摘要和公网逐字节证明，restarted=[]。
- TT代码 /mnt/data-disk/pipeline-dispatch/tt-readback-fix-20261103T110515-374fdad0/tt_readback.py.before。
- FB原生SQLite /mnt/data-disk/pipeline-dispatch/maintenance/unblock-20261103/fb-before-receipt-recovery.sqlite3。
- TT原生SQLite /mnt/data-disk/pipeline-dispatch/maintenance/unblock-20261103/tt987-backup/tt987-before-a0cd84aafff44f3296ad0feecf7ca1c5.sqlite3。
- FB冻结plan SHA256 92b87e8ae7916094083086ae3134edd55ef217f9a46b1f06036ec89e06f77ae4；TT987 plan SHA256 8ad0c51dbc954da11557f9c45f486cac1ea146726ed2602723ffe446ecb03766。具体证据和结果在同maintenance目录，权限0600，不入Git。

## 重启与实际验证

只自然重启 tt-auto-post-service.service（60384→3741525）；其三个timer恢复之前状态。coordinator1304161、CPU node1130766、GPU node248464、main API2666330保持。无强杀，门禁已移除，failsafe timer取消，cleanup_errors=[]。

TT旧/health为503（旧自动入口runner.path已被统一池接管停用），不能把该响应当成新代码未加载。原deploy.json保留file_replaced和HTTPError原始事实；19:07原定时回查3条均清空旧错误并保留原身份。补充verify-only证据写旁路文件，禁止通过再次重启来美化原部署日志。

## 精确回滚程序

1. 静态仅从 monitor-static-20261103T105653Z/main 与 nginx 子目录原子恢复各3文件，先JS/CSS后HTML；不重启任何服务。
2. TT仅在没有中央/原生TT在途及有效claim、HTTP请求自然排空的窗口停止 tt-auto-post-service.service；将上述 tt_readback.py.before 原子恢复到 /opt/tt-auto-post/current/features/pipeline_dispatch/tt_readback.py，保持owner/mode，编译检查后启动该单一服务。恢复原timer状态，撤除自己创建的门禁。不能杀正在进行的核对或发布。
3. 不回滚数据库。FB已经证实的平台发布事实、TT987已知失败、原素材、去重/回执及当前容量保留；SQLite备份只作审计与事故恢复参考，不覆盖实时账本。

技能上下文未改；本次一次性恢复和UI显示规则均保存在仓库文档，未修改记忆文件。

## 补充验证

19:12:07 使用 GitHub e06fdbdd760fd6fdb1588314798f969a45f195d8 的 --verify-existing 完成已上线实例验收。/internal/pipeline-dispatch/tt/reconcile 对987只读回执HTTP200且与原生库一致；新文件、旧备份、PID、timer与门禁全通过。postverify.json与原deploy.json同目录，原审计SHA256 a1faa4cf2f2b6a64888e32b735351aecd7192ae89f42b5b59d27c57d5daec245保持，restarted=false。当前旧 /health 仍有runner.path停用提示，未为消除该提示恢复旧执行器。
