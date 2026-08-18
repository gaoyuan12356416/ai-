# 部署文档

## 变更内容

素材池图片与软删除视频规则、共享图片媒体路径、历史错误重检。

## 配置项

无新增配置。

## 数据库变更

无 schema 变更；上线前执行在线 SQLite 备份和一致性校验。

## 部署步骤

1. 核对 GitHub 提交、active release、main composite 哈希和 ledger。
2. 暂停五个 X schedule/manual/auto timer，等待现有 oneshot 自然结束。
3. 在线备份 SQLite；Token 只记录 hash/mode/owner；备份代码/static/unit。
4. 从 GitHub 精确提交构建 immutable release并跑服务器测试。
5. 切换 Sidecar release，同步 exact Git 文件到 main runtime，窄重启 Sidecar/Main API。
6. 验证 health、匿名 401、SQLite、文件哈希和 ledger 不变量，恢复 timers。
7. 只观察自然 `no_due/no_pending`；禁止 run-now、手动任务或 canary Post。

## 验证步骤

### 2026-08-18 生产结果

- GitHub 提交：`c7cadcbcef1cea6040e54538d56acd619207df63`。
- Active release：`/mnt/data-disk/x-post-automation/releases/c7cadcbcef1cea6040e54538d56acd619207df63`。
- 备份：`/mnt/data-disk/x-post-automation/backups/20260818-155750-image-deleted-material`。
- 服务器聚焦测试：selector 18、pool 10、service 43、daily 62、relay 15，全部通过。
- Sidecar、X Auto sidecar、主 API 均为 active；Sidecar `/health=ok`，X Auto `ok=true`。
- release 与主 API 的 selector/service SHA-256 分别一致。
- SQLite：`quick_check=ok`、FK=0、active queue=0、active manual=0、unknown=0。
- 五个定时器已恢复：material manual、schedule claim、schedule、X Auto runner、X Auto scheduler。
- 生产源库只读样本：`4606498 -> image`，`4516470 -> video`，均无 selector rejection。
- 历史通用错误 87 条完成无发帖重检：79 条清除旧错误；2 条改为时长缺失；1 条素材不存在；5 条来源标签不安全。
- 未创建 run-now、manual、canary 或真实 X Post。

首次发布脚本因错误的长提交哈希在切换前停止；第二次因 X Auto 健康端口启动竞态触发自动回滚。两次均恢复旧 release/main 文件和定时器，第三次加入健康等待后成功。

## 回滚方案

切回部署前 release `3c067dbe3a5b18ef5c34adb3ff373408604bca56`，从上述备份恢复 main selector/service，重启 Sidecar、X Auto sidecar、主 API，并恢复五个 timers。默认保留当前 SQLite 和 Token；若明确需要撤销本次历史错误重检，再单独评估备份数据库中的 87 行差异，不整库覆盖。

## 注意事项

部署窗口如出现新生产提交，必须重新基于 live release 合并后再发布。
