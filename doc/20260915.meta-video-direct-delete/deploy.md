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

## 实际上线记录

- 2026-09-15 16:28至16:30（Asia/Shanghai），GitHub提交1b86d2afc3859ab4d70a0693226fe0873ebbac79，分支codex/meta-video-direct-delete-20260915，推送及服务端精确fetch均已验证。
- Release：/mnt/data-disk/meta-ad-asset-delete/release-1b86d2afc385。
- 备份：/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T082811Z-1b86d2afc385。11个目标基线/部署哈希一致，在线SQLite完整性ok。
- 服务器Python3.9.6运行177项测试全部通过，修改模块编译通过。
- 9个相关timer上线前全部active；暂停后相关发布service自然空闲，无资产执行/预览/重核。YouTube无publishing记录，未动其既有scheduled/unknown记录。
- 仅主API重启，新PID2986137，active/running；9个timer均恢复原active状态，保留主目录数据盘符号链接。
- 公网HTML/JS/CSS全部200且字节SHA256与GitHub对应文件一致；topbar200；未登录products/jobs401。启动日志无Traceback/SyntaxError/ImportError。
- 使用原任务所有者既有有效会话，只GET原任务：Video共34个全部video_direct_eligible=true；status=pending返回34，status=blocked返回0。台账仍保留历史blocked及原原因，领取时才写新的尝试与审计。
- 上线只读检查期间全部对象行和全部尝试行与备份逐行一致，未发起Meta DELETE。原任务保持17个Ad成功、16个Creative成功、1个Creative失败、34个Video历史阻止且现可直接执行。
- 服务器证据位于本备份目录：plan.json、hashes.json、service-state-before.json、service-state-after.json、public-verification.json、authenticated-verification.json。

精确回滚：保存并暂停原active发布timer，等待发布service自然空闲并确认无资产任务/重核，然后执行：

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-1b86d2afc385/scripts/deploy_meta_video_direct.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T082811Z-1b86d2afc385
systemctl restart drama-material-api.service
```

恢复这次保存的active timer，检查公网/服务健康。回滚代码到00c887238010对应基线，保留当前任务数据库，不恢复备份台账。
