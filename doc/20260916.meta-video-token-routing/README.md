# 发布队列 Token 匹配修正

原账户 Video 删除路径优先使用源广告的发布用户。发布用户并不总是实际 Token 用户：发布队列可能指定产品默认 Token。

## 匹配规则

从冻结的账户、源广告、产品及源记录，精确关联 `ads_facebook_auto_created_data.publish_queue_id = ads_template_make_queue.id`，再读取该广告产品的 `ads_apps_setting.default_user`。

- 队列 `default_token=1`：使用对应产品当前 `default_user` 的 `ads_facebook_info.accessToken`。
- 队列 `default_token=-1`：使用队列发布用户自己的 `ads_facebook_info.accessToken`。该表以 `-1` 表示“不使用默认 Token”，并非布尔 0。
- 每次执行重新读取队列、产品设置及指定凭证；旧预览也适用。当前默认用户可不同于预览时默认用户，但必须有完全匹配的冻结源广告、队列和产品证明。
- 队列缺失、源广告与队列的产品/用户不一致、0/NULL/未知标记、默认用户未配置、指定 Token 缺失或重复时，明确失败，不退回其他候选用户。0 的历史语义未经确认，不能当作“否”。
- 不新增 Meta GET 预检；每账户/Video 仍只发送一次既定 DELETE，不自动轮换 Token，不自动重试旧失败。已有成功、未知结果和去重回执保持。

新增安全诊断字段记录发布队列 ID、默认 Token 标记、产品、源发布用户、实际 Token 用户及精确源行。Token 只在内存中使用，不进入日志/台账/页面。凭证读取失败同样保存选定来源，后续账户继续处理。

## 验证

本地 `python -m unittest discover -s tests -p 'test_fb_ad_asset_delete*.py' -q`：303 项通过。
`node --test tests/test_fb_ad_asset_delete_account_ui.js`：16 项通过。
覆盖两条规则、当前默认用户变更、旧预览、跨账户/产品错配、缺失/未知配置、每次重新读取、实际请求所用 Token、发送前台账、失败后继续、历史成功不重发及秘密不落盘。

生产验证仅使用 `scripts/meta_video_token_routing_probe.py` 的真实选择路径，网络适配器禁止所有 Meta 请求。验证截图记录与默认 Token 记录；队列缺失等异常由隔离测试验证，不启动删除任务。

## 部署和回滚

使用 `scripts/deploy_meta_video_token_routing.py`，基线运行提交 `8798f8b098c3cd5f2e0b881c6a196399f704bff6`。仅同步 Source/Graph/Service/Store 及 HTML/JS，两份静态文件保持一致；保留 `20260915retired` 导航版本，不改变其他模块。无需 SQLite 迁移。

GitHub 精确提交检出后，服务器运行相同测试和只读 probe。确认删除/预览/重核空闲，prepare 备份所有目标文件与在线 SQLite，短暂停止主 API，apply 验证前后哈希，通过 `safe_restart_drama_api.sh` 窄重启主 API。验证服务、日志、双份静态/公网哈希、未登录 401、真实用户 GET 与所有旧台账行完全保留。

本次已现场确认 X/TT/FB 发布调度使用独立不可变 release 与 sidecar（8810、18831、18835 等），不访问主 API 8787。主 API 使用独立剧合成 worker。因此不暂停无关发布 timer，也不等待独立长任务；只记录并核对它们维持原状态。若未来拓扑改变，重新核实依赖并仅排空受影响调用。

回滚调用本 release 的 `deploy_meta_video_token_routing.py rollback BACKUP` 并窄重启。回滚会暂停旧版 Video 写入口，避免恢复错误 Token 选择；Ad/Creative 不受该 guard 影响。永不恢复旧 SQLite，不丢失成功回执或 unknown 锁。重新 apply 同一备份恢复本版。

## 上线验收（2026-09-16 11:02 北京时间）

- 分支 `codex/meta-video-token-routing-20260916` 已推送；运行提交 `e3411b7e93434ca5e21285fc81dd9acdce842ea8`，服务器精确 fetch/checkout 已验证。
- 主机 `43.166.187.96`，运行入口 `/root/drama_material_service`，保持既有数据盘符号链接。
- release `/mnt/data-disk/meta-ad-asset-delete/release-e3411b7e9343`。
- 备份 `/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260916T030229Z-e3411b7e9343`，含原 8 个部署目标文件、在线 SQLite 与禁止旧 Video 写入的回滚版本。
- 主 API PID `3106258 -> 3716183`，active/running；9 个发布 timer 始终 active，原 X 调度进程 `3701395` 未受影响。
- 服务器 Python 303 项回归及编译通过。8 个部署目标哈希、公网 HTML/JS 哈希与 release 一致；页面和 topbar 200，未登录 products/jobs 401，原所有者任务 GET 200；启动日志无导入/语法/Traceback 错误。
- 18 个任务、6,425 个对象、2,027 个对象尝试、1,448 个回执、1,077 个账户结果、1,080 个账户尝试及全部其他旧台账行与备份逐行一致；完整性/外键检查通过。
- 实际运行代码只读 probe：个人分支仍选择源发布用户；默认分支选择对应产品默认用户。精确队列/账号/用户证据仅保存在服务器备份目录的 `runtime-token-routing.json`。无 Meta 请求、无删除重试。
- 验收文件：`acceptance.json`、`runtime-token-routing.json`、`service-state-before.json`、`service-state-after.json`，均位于备份目录。

精确回滚（先确认没有删除、预览或重核在途）：

```bash
python3 /mnt/data-disk/meta-ad-asset-delete/release-e3411b7e9343/scripts/deploy_meta_video_token_routing.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260916T030229Z-e3411b7e9343
bash /mnt/data-disk/meta-ad-asset-delete/release-e3411b7e9343/scripts/safe_restart_drama_api.sh
```

保留当前台账，Video 写入口暂停；恢复新规则时将 `rollback` 改为 `apply` 并按同样流程窄重启。
