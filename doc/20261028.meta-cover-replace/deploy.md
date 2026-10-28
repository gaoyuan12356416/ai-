# 部署与回滚

状态：2026-10-28 18:27:43 Asia/Shanghai 已切换代码，持续消费尚未开启；只读预览及首批真实验收进行中。

## 目标

CPU 43.166.187.96，运行目录 /mnt/data-disk/meta-violation-material-cleanup。GitHub gaoyuan12356416/ai-，releases/<sha> 为固定提交，current 指向该发布。

新增 meta-cover-scan.service/.timer、meta-cover-worker.service、meta-cover-notify.service/.timer。安装器 deploy/install_cover.py 提供 prepare/apply/rollback；参数以对应提交 --help 为准。

旧 meta-violation-material-cleanup.timer 保持停用。保留旧代码、任务、凭证和历史回执；新流程不调用旧 run.py live 删除入口。

## 发布顺序

1. 完成本地测试和 diff 检查，提交并推送 GitHub，记录 SHA。
2. 核实服务器版本、进程和锁。停止旧任务的新调度，等待在途写入排空并确认旧进程退出。
3. 从 GitHub 拉取精确 SHA；prepare 备份代码链接、配置、unit/timer 和账本，SQLite 使用一致性备份。
4. apply 切换并安装新单元；首次验收前保持持续消费关闭。配置 cover_live_enabled，旧删除 timer 不启动。
5. 独立 preview 核对源范围、语言、完整链接及 payload；scan 入队，work --once --max-ads 256 处理自然命中首批。
6. 保存峰值/吞吐/限流指标、绑定读回、删除意图与回执、未知隔离及 Feishu 回执。确认无重复写入或参数漂移再启动持续 worker 和小时 scan timer。
7. 读取服务/定时器状态与下一触发时间、剩余积压，补充发布回执。

32 是上限，单账户 2、24 次/秒、账户分布和限流会影响实际并发。零候选必须记为未完成真实写入验证，不能称作成功执行。

异常重复写入或参数变化触发 cover-write-paused.json。排查只读核实，不通过清账本或删除围栏强制重放。

## 回滚

停止新 scan/notify timers 和 worker，确认在途请求排空；使用 installer rollback 或恢复备份代码链接、配置和单元。继续保留共享 Store 的增量互斥能力，避免旧入口忽略新未知围栏。旧删除 timer 保持停用。

保留最新 cover.sqlite3、共享 tasks.sqlite3、cover-audit 和未知状态；绝不以旧 SQLite 备份覆盖进度。成功绑定不会随代码回滚自动恢复；已删除 Creative 无法恢复旧 ID。业务素材恢复需另行制定可核实操作。

## 发布回执

- GitHub SHA：a37148ebf5d02588137c67a905fe6fb8da7a3a2b，分支 codex/meta-cover-replace-20261028 已推送。
- current：/mnt/data-disk/meta-violation-material-cleanup/releases/a37148ebf5d02588137c67a905fe6fb8da7a3a2b。
- 旧版本：869a183a8402ef2bceb0417e41af6c9118d9e606。
- 备份点：/mnt/data-disk/meta-violation-material-cleanup/backups/cover-20261028T102157Z-a37148ebf5d0；含代码链接、配置、旧 unit/timer、1,907,179,520 字节共享账本及 14,347,395,072 字节旧运行账本，SQLite integrity_check 和文件 SHA256 完成。
- 安装器 transient unit meta-cover-install-a37148.service：Result=success、ExecMainStatus=0。
- drama-material-api.service 已重启并 active；受保护 API /api/fb-post-ad-delete/jobs 返回预期 401 cookie_auth_required。
- 旧 meta-violation-material-cleanup.timer 仍 disabled/inactive；新 worker 在首批验收前 inactive。
- 首批自然命中、处理数、积压：
- 替换成功/失败/未知：
- 删除成功/失败/未知/引用暂缓：
- Graph 实际峰值、吞吐、限流：
- Feishu 回执：
- 新服务/timer 状态和旧删除 timer 停用证据：

首个只读预览：477.454 秒完成扫描，47 个规范素材（57 个产品/素材关系），候选 6,170 Ads，跳过历史确认已删 Ads 7,690、路由不明确关系 1,934；无 Meta 写入。64 条参数预览最初全部被严格校验拒绝。真实 GET/只读源库查明：截图来源为 OPT_OUT 默认配置，2 个 object_id 与 page_id 完全相同，18 条来源差异均为已支持的 HotDrama 复用 DramaWave 剧素材，且对应根产品/剧 ID/语言封面唯一。已添加原样保留和精确跨产品映射的回归；仅修复这些已确认形态，其他复杂媒体模式继续逐条失败。

5d618a0 复核为 64/64 参数有效、绑定一致；正式扫描批次 cover-20261028T184730-996d0cf9 共 6,170 候选，485.186 秒。首批 256 条于 18:58:07 完成：129 obsolete、36 waiting_review、91 failed，尚余 5,914 pending。86 个资源均明确拒绝（code 3），无新 Creative ID、绑定意图或 DELETE；参数替换验收尚未通过。峰值在途 9（配置上限 32），427 次 HTTP 响应、68.175 秒，无限流降档。飞书已送达，回执 om_x100b61291d2390a8c060e94fd2edd75。不得重置这些失败任务进行重试。

Meta validate_only 对照校验（不创建对象）确认 enable_direct_install=False 触发 neko_direct_api_enable；省略它后，Meta 要求移除已经停用的 standard_enhancements 聚合字段。保留完整逐项增强配置、仅省略上述两个提交字段后，校验返回 HTTP 200 / success=true / 无 ID。账本及回读预期保留原完整配置，创建后须严格验证再绑定。诊断回执位于 runtime/validation-20261028/；继续验收仅选尚未执行候选。

回滚命令（在 CPU 上运行，保留最新账本与共享围栏）：

```bash
python3 /mnt/data-disk/meta-violation-material-cleanup/releases/a37148ebf5d02588137c67a905fe6fb8da7a3a2b/server-monitors/meta-violation-material-cleanup/deploy/install_cover.py rollback /mnt/data-disk/meta-violation-material-cleanup/backups/cover-20261028T102157Z-a37148ebf5d0
```

2026-10-28 回读修复：真实创建已被接受；4 个已知 ID 因 Meta 自动追加 Creative 管理名称后缀、GET 省略退役 standard_enhancements 聚合而隔离。仅允许原名称完整前缀加精确日期/32位十六进制后缀；仅在合法创建投影已省略聚合且逐项配置齐全时允许 GET 缺失聚合。广告标题、文案、完整链接、身份及逐项增强仍严格核验。110 项封面回归通过。旧失败任务不重置，未知对象只读核对后仅恢复首次未发送绑定。

绑定阶段真实样本确认：Meta 会将 post_engagement/post_interaction_gross/link_click 的旧帖子 ID 替换为新 Creative 的 effective_object_story_id，并重排 tracking_specs。仅当新旧有效帖子 ID 归属同 Page 且完整 tracking 多重集合（含重复数、Pixel、转化 ID、额外字段）一致时接受。绑定未知只读重新取 Ad/Creative 并验全参后确认，不重发 POST。111 项封面回归通过。

用户于本会话明确确认“允许该视频专用项不再回传，继续修复和替换”。仅允许已核实图片 Creative 的 video_auto_crop 不回传（原值必须为严格 OPT_IN/OPT_OUT 结构），发送仍保留原值，返回不同值仍拒绝，其他逐项功能缺失仍拒绝。封面回归 112 项通过。

真实样本发现 effective_object_story_id 可在绑定后才生成，故绑定后必须重新 GET 新 Creative 并验全参，再核对新旧帖子引用；仍未就绪时仅保留未知，禁止重发绑定。新增真实 SQLite worker 行为测试覆盖绑定后才有帖子 ID、延迟就绪后 GET-only 确认且仅一次 POST。封面回归 113 项通过。
