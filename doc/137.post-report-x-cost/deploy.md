# 部署与回滚
GitHub拉取准确提交至/mnt/data-disk/post-daily-report/releases/<commit>，运行68项测试、编译、systemd-analyze verify及相同ProtectSystem=strict隔离环境非发送预览，然后scripts/install_post_report_x_cost.py --apply。
安装器要求当前8a0aad86d96722d61094176ee2f7be62de8f944e及旧代码/unit哈希一致，锁定run.lock，在线备份delivery及两X SQLite，保存unit/前版本/rollback.sh。仅暂停恢复日报timer，切日报链接/unit。publisher unit、token和业务库不改。daemon-reload后验证隔离预览、月报和下一次10点触发/quick_check。
回滚：flock /mnt/data-disk/post-daily-report/run.lock bash <backup>/rollback.sh。仅代码/unit，绝不覆盖新发布事实。发送未知先核对平台，不换revision重发。
