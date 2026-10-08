# X日报修复与成本上线记录

已部署 GitHub 提交：9b9baf7882bcce3a9e0b199c3a8697d0027d322a。
主机：43.166.187.96。
代码路径：/opt/post-daily-report/current → /mnt/data-disk/post-daily-report/releases/9b9baf7882bcce3a9e0b199c3a8697d0027d322a。
旧版本：8a0aad86d96722d61094176ee2f7be62de8f944e。
备份：/mnt/data-disk/post-daily-report/backups/20261008T071011Z-x-cost。

本地与Linux各68项测试通过；编译、git diff --check、systemd-analyze verify通过。上线前/后在与实际日报相同的ProtectSystem=strict环境中复算成功。日报unit已daemon-reload，日报timer恢复active，下一次北京时间2026-10-09 10:00。不重启发布服务或改变发布定时器。

10月7日报X：应发137、已发4、补发0、未完成133；费用估算USD0.800。9月估算USD439.210（非实际账单）；带URL2141、无URL7、目标Repost727，结果不明请求1未计入。

补发修正版：revision=x-plan-cost-20261008；message_id=om_x100b63406f05e8acc2935b5d33e7705。飞书回读code=0，chat和内容一致；重跑already_sent。原日报文件/回执SHA256未变；publisher状态未变；两个X库quick_check=ok。

回滚命令（在CPU服务器执行）：

```bash
flock /mnt/data-disk/post-daily-report/run.lock bash /mnt/data-disk/post-daily-report/backups/20261008T071011Z-x-cost/rollback.sh
```

只还原日报代码和unit；保留所有投递、发布、Token和修正版账本，不恢复旧数据库。

技能上下文：本地AI后台和X发布技能追加了日报只读统计与费用估算契约；未修改个人记忆。
