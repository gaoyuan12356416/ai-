# 部署与回滚

状态：部署前草稿，实际提交、备份目录与结果待执行后填写。

## 目标

主机 43.166.187.96，运行目录 /mnt/data-disk/meta-violation-material-cleanup，服务 meta-cover-worker.service。旧版本为 4c7cd30878ad46dd2908839a60dc09642ab2eda9。扫描/通知仍沿用既有服务。

当前停止证据为 2026-10-29 14:15:28、ad_parameters_changed_after_bind。该未知结果不能直接重放。

## 顺序

1. 本地完成编译、针对性/全模块测试和 diff 检查，提交并推送 GitHub。
2. 确认数据盘 UUID 3e8ac4e8-7770-456d-9e89-2ec5dd405fa8 和容量，用指定 GitHub commit 创建候选 release。
3. 停止并排空 worker，保存 current 指向、unit/config、暂停证据、SQLite 一致性备份及共享账本，记录状态计数。不可直接复制活跃 SQLite 主文件代替一致性备份。
4. 运行候选 Linux 测试和 hash 核对；只读核对原暂停原因，仅证据和修复支持时解除全局暂停，未知/失败任务不复位。
5. 切换 current，按需 daemon-reload，只启动相关 worker，不恢复退役模块。
6. 观察两个 lane、广告出队、清理回执、共享预算和错误日志；保存前后可比样本。

## 回滚

先停止并排空 worker，切 current 回旧 release，恢复本次修改的 unit/config，再重启服务。保留最新任务/资源/共享一次删除账本和所有回执，不回滚业务数据库、不恢复已消费写入意图。

旧代码将恢复同步清理性能行为。尚未解决全局暂停条件时保持停止。已验证的具体命令与备份位置在部署后补充。
## 已核对的安装器命令

在指定 GitHub release 的 `server-monitors/meta-violation-material-cleanup` 目录中运行，`<release>` 和 `<backup>` 均替换为安装器核对后的绝对路径：

```bash
python3 deploy/install_cover.py prepare <release> --active-ledgers-only
python3 deploy/install_cover_worker.py apply <backup>
```

`--active-ledgers-only` 前提是旧清理 service/timer 已停用，安装器会核对并备份活动共享/封面账本，旧历史账本保留原位。`apply` 切换代码和 worker unit，不重启主 API。

精确回滚入口：

```bash
systemctl stop meta-cover-worker.service
python3 <release>/server-monitors/meta-violation-material-cleanup/deploy/install_cover_worker.py rollback <backup>
```

安装器回滚保留最新账本并保持执行停止。核对状态和未决结果后，才决定是否启动旧 worker。实际 SHA、备份路径和验证结果待部署后填写。