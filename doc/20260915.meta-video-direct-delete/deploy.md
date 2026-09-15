# 部署与回滚

43.166.187.96；逻辑/root/drama_material_service保留数据盘符号链接；单元drama-material-api.service；公共静态/usr/share/nginx/html。

## 流程

1. 本地验证，推送codex/meta-video-direct-delete-20260915，服务端从GitHub精确检出至/mnt/data-disk/meta-ad-asset-delete/release-<SHA12>。
2. 服务端Python3.9.6模块回归/编译。
3. scripts/deploy_meta_video_direct.py prepare RELEASE按00c887238010基线核对5个feature与双份HTML/JS/CSS（11目标），在线备份最新SQLite并校验完整性。
4. 保存9个既有发布timer原状态，仅暂停active项；等待相关service自然结束，确认无活跃资产任务/recheck。
5. apply BACKUP并仅重启主API，恢复原active timer。静态版本20260915-meta-video-direct-delete。
6. 只读检查服务/日志/公网哈希/未登录401/真实会话原任务GET；对比尝试/成功记录/固定清单。

## 回滚

同样排空，然后从本次release执行：
python3 scripts/deploy_meta_video_direct.py rollback BACKUP
systemctl restart drama-material-api.service

保留当前SQLite、回执、unknown锁与历史索引，不恢复备份数据库，不开启旧Post接口。恢复原timer并验健康/双静态。

实际提交、备份及只读验收部署后回填。
