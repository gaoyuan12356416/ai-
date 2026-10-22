# Facebook 手动和 Excel 发布黑名单豁免

用户要求取消手动/Excel 发布的剧目黑名单限制。手动素材选择、预检、提交复核、后台执行均豁免 content_id 与 series_code 黑名单。默认 SourceRepository 仍检查黑名单，Page 素材池入池/调度/执行及旧自动发布保持原政策。服务端按 task.source_kind 和冻结 pool 字段选择执行政策，不接受请求覆盖。

不改历史批次，不重试失败任务，不提交测试帖。旧预览需要重新上传校验，以包含之前被跳过的行。

## 保留限制

- 素材仅来源6、Dramawave、视频type=2、未删除；有效 content_id、名称、语言、视频URL；时长大于0且不超过3600秒。
- 剧目 app1479、release_status=1、deploy_time>0且已到上线时间；同语言唯一剧名。使用 desc 宏时须有唯一简介。
- 素材视频仅批准的COS/CDN主机mp4，HTTPS（原COS HTTP会规范化）；源身份在执行前保持一致。
- Page已配置、在用户可访问授权范围，有有效Token；素材语言与Page配置一致。个人Token模式只能使用操作人自己的Page授权。
- Cookie登录、模块/入口权限、唯一归因邮箱到sub_user_id映射。
- 相同Page+素材活动占用及Page未决结果阻断；执行时共享Page串行锁；历史成功素材须确认重复发布。无手动14天冷却规则。
- 每批最多200配对，Excel最多200数据行、2MiB、固定8列表头、禁止公式/宏/加密、长ID精度保护。同一Excel重复Page+素材跳过。
- 预约必须未来时间（Excel无时区视为北京时间）；每日全局新模块额度按北京时间日期计数，当前1000条，非取消任务计入，含手动与池任务。
- 文案必填、仅name/desc/url宏；模板16000 UTF-8字节、展开5000字节；Excel标题255字符/1020字节。
- 15分钟预览签名、配置版本、素材与历史事实变化要求重新预览；操作ID幂等；随机加工需开关及合格成片，不降级原片；短链唯一和公网读回通过。
- 总发布开关、Excel/V2开关、共享GPU加工容量及平台速率限制保留。

## 验证和交付

以 fd25d7e 为已核对线上四文件基线。仅安装 features/fb_video_publish 的 sources.py、service.py、excel.py、worker.py 至主API和FB runtime（8个文件），不重启主API，不改数据库结构/静态/GPU服务。

先运行相关 source/core/Excel/pool/broker/worker测试。`scripts/verify_fb_manual_blacklist.py` 使用真实只读SQL broker比对默认/手动来源，并通过运行中sidecar执行Excel预览；操作ID查库证明未创建批次。

部署：GitHub精确提交检出后执行 `python3 scripts/deploy_fb_manual_blacklist.py` 演练，随后同命令加 `--apply`；部署器校验基线SHA、排空、在线备份、只重启FB sidecar并恢复原timer/worker状态。

回滚：同release执行 `python3 scripts/deploy_fb_manual_blacklist.py --rollback <本次manifest.json>`。须先对账未完成手动任务，避免恢复黑名单后拦住新受理任务；不恢复旧SQLite，不删除短链。

## 2026-10-22 17:19 北京时间上线验收

- 实现 `6d11214`，实际部署提交 `7b64d8353781575d0e5797ed52469577517b1275`，已推送 GitHub 分支 `codex/fb-manual-blacklist-20261022`。
- CPU `43.166.187.96` GitHub release `/mnt/data-disk/fb-video-publish/releases/7b64d83`；安装目标 `/opt/fb-auto-post/current/features/fb_video_publish` 和 `/root/drama_material_service/features/fb_video_publish`，8个文件SHA核对通过。
- 截图可读的6个素材真实来源比对：默认规则6个全部命中黑名单；手动规则6个全部通过。运行中HTTP Excel preview从0有效/6跳过变为6有效/0跳过。未创建批次、未调用Graph发布。
- 生产5项专门回归通过；本地source/core/Excel/pool/broker及自动来源回归均通过。现有51任务冻结字段、51短链保留。无数据库迁移。
- FB sidecar窄重启，worker/scheduler和9个timer原启停状态恢复；主API PID保持3770172。健康通过，未登录公网API保持401，部署后error级日志0行。
- 只读证据：`/mnt/data-disk/fb-video-publish/evidence/manual-blacklist-{before,after,deployed}-20261022.json`。

准确回滚命令（先对账未完成手动任务，保留现库）：

```bash
python3 /mnt/data-disk/fb-video-publish/releases/7b64d83/scripts/deploy_fb_manual_blacklist.py --rollback /mnt/data-disk/fb-video-publish/backups/page-list-20261022T091917Z-7b64d835/manifest.json
```
