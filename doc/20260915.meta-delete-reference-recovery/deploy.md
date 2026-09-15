# 部署与回滚

主机43.166.187.96；主API/root/drama_material_service、drama-material-api.service；静态同步/usr/share/nginx/html。

GitHub-first：本地验证→提交推送→服务器精确检出→真实只读索引probe→基线哈希核对及在线SQLite备份→排空受影响任务→切换代码/双份静态→仅重启主API→公网/原任务只读检查。

工具：scripts/deploy_meta_asset_recovery.py prepare RELEASE；apply BACKUP；rollback BACKUP。仅选定6个feature文件与HTML/JS/CSS。校验当前内容与GitHub基线e18d88f，漂移停止；回滚保留全部当前任务和索引数据，绝不恢复旧任务SQLite覆盖进度。主app和共享导航不变。

数据盘：/mnt/data-disk/fb-ad-asset-delete/video-reference-index；UUID须正确。索引不完整或过期时保持阻止；默认源构建/快照窗口600秒，单连接FIFO Gate，30GiB索引上限及2GiB剩余空间门槛。没有新增timer。

只读性能探针：python3 scripts/meta_video_reference_probe.py --job-id <已存在任务>。报告写数据盘reference-probe.json；不导入app、不调用Meta删除、不修改任务对象。

## 实际上线记录

- 2026-09-15 15:40左右（Asia/Shanghai）完成部署；GitHub精确提交`00c8872380102e8ca7423f80cea48806a2d71295`，分支`codex/meta-delete-reference-recovery-20260915`，release `/mnt/data-disk/meta-ad-asset-delete/release-00c887238010`。
- 备份 `/mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T073842Z-00c887238010`。12个目标基线/上线SHA256逐个一致，包含双份静态和在线SQLite，完整性检查ok。
- 保存并保留9个相关timer的原状态（本次切换前均inactive），确认相关发布service均无运行任务，资产任务无previewing/running后，仅重启主API；新PID2951131，active/running。首次排空检查的Python模块路径错误发生在停止服务前，修正后完成；无代码半部署。
- 主目录已被其他维护任务迁到数据盘：逻辑`/root/drama_material_service`指向`/mnt/data-disk/root-storage-20260915/rootfs/root/drama_material_service`；本次保持该兼容链接及其他任务的timer状态。
- 公网HTML/JS/CSS均200且哈希与GitHub一致；topbar200；未登录products/jobs均401。页面版本`20260915-meta-assets-reference-recovery`。浏览器确认未登录门禁；登录后完整页面流程以DOM/API模拟测试和真实授权接口核验，本次未在登录态浏览器触发删除。
- 原任务`e9d2535ee6f94a5389f15483d77b7740`通过真实任务所有者现有Cookie提交只读recheck；操作`62507b3732464bcdadf0dcfbec6a4bd7`返回202，相同request_id重复提交返回同一操作且duplicate=true。
- 最终完整索引4,849,751行、4,906,116条解析关系，用时344.37秒；FORMAT_VERSION=2识别81条达到512字符的历史记录，分布34个账户。它们属于全局引用核查范围，原任务删除清单保持68个对象。
- 15:48:04核验完成：34/34已核验，0个解除阻止。首个明确阻断为账户3596231830449606、Ad23847146011620786、源行3435414，Meta200/HTTP403提示账户所有者未授予ads_management或ads_read访问。81条异常尚未全部获得实际关联证明，不能宣称恢复一个账户后必然全部放行。
- 新增DELETE请求0：执行尝试全部30行与备份逐行一致，成功对象全部行一致，冻结ID/产品/账户/素材关系一致；仅阻止原因、核验结果和时间更新。原任务保持17 Ad成功、3 Creative成功、14 Creative失败、34 Video阻止。
- 服务器证据：本备份目录的`public-verification.json`、`service-state-before/after.json`、`recheck-accepted.json`、`recheck-idempotency.json`、`creative-readback.json`、`recheck-final.json`。仓库脱敏汇总见`production-verification.json`，操作说明见`operator-result.md`。

当前代码回滚：先排空相关任务，执行`python3 /mnt/data-disk/meta-ad-asset-delete/release-00c887238010/scripts/deploy_meta_asset_recovery.py rollback /mnt/data-disk/meta-ad-asset-delete/recovery-backup-20260915T073842Z-00c887238010`，再仅重启主API，核对哈希/健康并恢复原本active的timer。保留当前任务台账、索引及关闭的旧删除路由。
