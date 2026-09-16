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

生产验证仅使用 `scripts/meta_video_token_routing_probe.py` 的真实选择路径，网络适配器禁止所有 Meta 请求。验证截图记录、默认 Token 记录及发布队列缺失记录；不启动删除任务。

## 部署和回滚

使用 `scripts/deploy_meta_video_token_routing.py`，基线运行提交 `8798f8b098c3cd5f2e0b881c6a196399f704bff6`。仅同步 Source/Graph/Service/Store 及 HTML/JS，两份静态文件保持一致；保留 `20260915retired` 导航版本，不改变其他模块。无需 SQLite 迁移。

GitHub 精确提交检出后，服务器运行相同测试和只读 probe。保存原启用发布 timer 状态，短暂停止这些 timer，等相应 service 与删除/预览/重核自然空闲。prepare 备份所有目标文件与在线 SQLite，apply 验证前后哈希，然后通过 `safe_restart_drama_api.sh` 窄重启主 API，在 finally 恢复原 timer。验证服务、日志、双份静态/公网哈希、未登录 401、真实用户 GET 与所有旧台账行完全保留。

回滚调用本 release 的 `deploy_meta_video_token_routing.py rollback BACKUP` 并窄重启。回滚会暂停旧版 Video 写入口，避免恢复错误 Token 选择；Ad/Creative 不受该 guard 影响。永不恢复旧 SQLite，不丢失成功回执或 unknown 锁。重新 apply 同一备份恢复本版。
