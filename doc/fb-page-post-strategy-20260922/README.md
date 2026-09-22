# FB Page 分档频次与 Post 效果选材

同素材 ID 冷却无法阻止同一 Page 连续发布一部剧的不同剪辑。原模板也为所有 Page 设置相同频次，且只依据付费素材指标选材。本次增加 Page 每日次数、同剧滚动冷却、固定时段内错峰，以及可保留对照的 Post 效果选材。

## 配置与行为

- `cooldown_days` 保持原素材冷却规则；`drama_cooldown_hours=24` 在规划事务内检查前后 24 小时预约，并在提交前用真实发布完成时间复核。缺同语言候选就跳过，不自动缩短冷却或跨语言补量。
- `page_daily_limits` 保存 Page ID、名称、A/B/C/hold 标签与每日上限；`default_daily_count=0` 暂停未分类新增 Page。实际数量取该上限与每日时段数的较小值，缺授权和未知发布仍受原门禁约束。不会修改 Page 组成员。
- `stagger_minutes=40` 将每个 Page 的计划时间稳定分散到固定时段后 0–40 分钟，不跨下一时段或北京时间零点。减少次数的 Page 分散到各时段，不全部挤在最早时段。
- `feedback_selection={enabled:true,rollout_percent:50,exploration_percent:20}` 约为全部任务 20% 同语言按剧均匀探索、40% 原消耗/ROAS 排序对照、40% 效果选材。分配由 Page 与时隙稳定散列冻结，实际比例随有限样本浮动。
- 效果样本仅使用本组自动帖，满 48 小时、近 21 天发布、72 小时内查询；素材或剧需至少 5 条且跨至少 3 个 Page。点击/千次媒体浏览加 1000 展示平滑，先分别计算 Page，再取中位数，防止单 Page 爆款主导。前三个北京时间自然日新用户收入仅辅助比较同点击水平，不伪称精确滚动 72 小时收入。收入不足或不可读取保持 NULL；样本不足回到原排序。
- 候选列表每剧最多 20 个素材，保留不同剧的可选项；素材、黑名单、上线状态、产品、语言和描述的原校验仍执行。
- 新增 `selection_json` 保存每条任务的 control/explore/feedback/fallback 与选择依据；新设置经现有停用→版本更新→启用流程应用。旧版本未提交的任务保留并标记跳过，已提交、已发布、未知结果和所有发布尝试保持原样。当天新启用按原规则从下个完整北京时间自然日排期。

## 只读效果采集

`fb-auto-post-effects.timer` 每小时调用 `scripts/fb_auto_post_effect_runner.py`，只读 Graph GET；不引用 GPU 或发布 API。先从已确认的 video ID 查询真实 `post_id`，再读取 Post Insights。不能将 video ID 拼接成 Post ID，也不能将 HTTP 200 的空指标当作零。

单次最多 750 帖、400 次 GET，0.5 秒间隔；每 Page 最多 3 个已有授权；API 使用率任一受限维度达到 85% 即停止，失败延后 6 小时。SQL 通过 `/usr/bin/mysql` 的共享 FIFO Gate，只访问 63350，校验 `@@read_only=1`，不用 `mysql.real` 或直连驱动绕过 Gate。凭证仅在进程内，禁止输出响应中的凭证与分页链接。`FB_AUTO_EFFECTS_PREFERRED_CREDENTIAL_ID` 可配置经实测具有 read_insights 的现有授权 ID，空值使用原顺序；不改变发布授权选择。

保存 24h、72h、7d 首次有效快照，允许目标时间后 6 小时窗口；错过窗口不补造历史快照。基线导入仅接受与发布台账 Page/video/真实 Post ID 一致的记录，作为 `baseline`。当前值每天刷新一次，固定年龄快照独立保存。收入等待前三个自然日结束并额外留三天回填，只统计已校验 task + Page + material 的站点 2049 记录。

## 验证与发布

1. `python -m unittest discover -s scripts -p 'test_fb*.py' -q`
2. `node --check static/fb-auto-publish-template.js`，`python -m compileall -q features/fb_auto_posts scripts/fb_auto_post_effect_runner.py`，`git diff --check`。
3. 在 SQLite 在线备份上演练配置版本切换、效果基线导入、下个自然日排期，并比较已发布/未知/提交中任务、ledger、attempts 的校验值。
4. 前端浏览器验证保存 145 行频次配置后仍为 526 条名义上限、14 天素材冷却、24 小时同剧冷却；保存失败保留表单。验证有权限与未登录状态。
5. GitHub 提交并 push，服务器 fetch 验证同一 SHA。备份将覆盖的准确文件、环境、timer 状态与 SQLite；暂停领取并等待活跃准备/提交租约结束，避免中断 Graph 写。
6. 仅替换 FB sidecar 的变更模块、新采集脚本和两个前端文件；前端同时更新 main runtime/static 与 `/usr/share/nginx/html`。保留线上其他文件，包括已发布的 GPU 模板、手工发布工具、导航和 runner 调度超时设置。
7. 只重启 `fb-auto-post-service.service`，启用新的 effects timer，恢复原 claimers。通过原管理 API 提交新模板版本并回读；确认自然 tick/plan 生成新版本任务，真实内容冷却、频次与错峰符合配置。

## 回滚

停止新增 effects timer，暂停 FB claimers，等待提交任务完成。恢复此次备份中准确的源码/静态文件和环境，重启唯一 FB sidecar 并恢复原 timer 状态。新规则需先经新代码停用并将旧配置保存为更高版本，再回滚源码。始终保留当前 SQLite、已发布事实、未知任务与公开短链；禁止拿旧数据库覆盖现有状态。

本轮不变更视频剪辑或引导叠层，视频引导作为独立内容试验，避免干扰频控与选材对照。
