# X Post 发布错误目录

## 口径

本目录覆盖素材池入池、自然排期、手动发布、媒体预检/修复、账号与 Token、队列台账、
X 上传/Post/Repost 的可见错误。它从生产基线
`3b9cda698e8f4ad1a025a8f9e2e1dd6296c95769` 及本需求变更代码提取。自动覆盖测试当前
从 8 个发布链路模块扫描到 232 个稳定 literal error code，本目录逐码覆盖，缺失为 0。

- `等待`：不是发布失败，不建队列；条件到达后由自然排期复检。
- `本轮跳过`：本次没有发布，后续自然排期可重新选材。
- `阻断`：数据、配置或账号问题修好前不能发布。
- `明确失败`：X 或本地已明确失败；默认不自动重发，避免重复 Post。
- `结果未知`：写请求可能已到达 X；必须人工核对 X 和台账，禁止自动重试。
- `invalid_request` 是 API 输入校验总码，具体字段原因在消息中。它不是运营发布结果，
  因此不把数百个字段级分支重复列为独立发布错误。

## 本次修复后的关键状态

| 错误码 | 中文含义 | 状态/系统动作 | 运营处理 |
| --- | --- | --- | --- |
| `drama_not_yet_deliverable` | 关联短剧尚未到权威 `deploy_time` | `等待`；页面显示“待可投放”，保持 `unpublished`、不绑定 queue；到点后的下一次自然素材排期重新读取源数据 | 无需删除、重加或手工改状态；如时间已过仍长期不动，检查自然排期 run |
| `drama_deploy_time_missing` | 没有可核验的 Dramawave 可投放时间 | `阻断` | 补齐短剧投放时间映射后重新校验 |
| `drama_deploy_time_invalid` | 可投放时间无效、平台映射不一致或多条权威时间冲突 | `阻断` | 修正源数据，不允许人工绕过 |

`drama_not_yet_deliverable` 只有在本轮只读 selector 返回的 `drama_deploy_time <= 当前时间`
时，才能在创建队列的同一 SQLite 事务中清除；历史错误本身不能授权提前发布。

## 素材与短剧源数据

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `material_validation_unavailable` | 源数据库暂时无法完成入池校验 | `阻断`；源查询恢复后重新校验 |
| `material_validation_incomplete` / `material_validation_failed` | 入池校验没有得到完整、可识别的结果 | `阻断`；查 selector/接口契约 |
| `material_not_found` | 素材 ID 不存在 | `阻断`；核对 ID |
| `material_not_found_or_ineligible` | 历史粗粒度错误：不存在、非有效视频或时长缺失之一 | `阻断但可复检`；新任务会给出更具体原因 |
| `material_source_ambiguous` | 同一素材 ID 有重复且不一致的源记录 | `阻断`；清理源数据歧义 |
| `material_product_mismatch` | 素材不属于 Dramawave | `阻断`；不能投到本流程 |
| `material_not_video` | 当前发布路径只接受视频，但素材不是视频 | `阻断但可复检`；选择正确素材/路径 |
| `material_type_unsupported` | 源数据媒体类型不是受支持的图片或视频 | `阻断` |
| `material_inactive` | 视频已删除或不可用 | `阻断但可复检`；恢复源状态后再排期 |
| `material_deleted_image_unsupported` | 图片已删除，当前规则不允许继续使用 | `阻断` |
| `material_metadata_invalid` | 素材 ID、语言、content_id、URL 等基础信息不完整 | `阻断` |
| `material_duration_invalid` | 源时长不是合法数值 | `阻断` |
| `material_duration_missing` | 视频时长缺失或为 0 | `阻断` |
| `material_duration_exceeds_limit` | 源时长超过当前路径上限（最高 4 小时） | `阻断` |
| `material_url_not_https` | 素材源地址不是 HTTPS | `阻断` |
| `material_source_tag_invalid` / `material_tag_invalid` | 标签无法安全解析 | `阻断`；修正编码/内容 |
| `material_source_tag_unsafe` / `material_tag_unsafe` | 历史版本曾因标签过滤素材 | 当前 X selector 不再读取或按 source/resource tag 过滤；旧状态经当前源数据复检后清空 |
| `material_language_not_scheduled` | 当前配置确实不存在该素材语言账号 | `本轮跳过`；保留素材，等同语言账号排期；已有该语言但本批容量满时不写此码 |
| `material_has_violation` | 历史版本曾按违规记录过滤 | 当前 X 合同不查询或按历史违规表过滤；旧状态经当前源数据复检后清空 |
| `violation_check_invalid` | 历史违规结果无法核验 | 兼容旧记录；查旧校验链 |
| `drama_mapping_missing` | 素材没有对应短剧信息 | `阻断` |
| `drama_mapping_invalid` | 短剧映射字段缺失或身份/语言不一致 | `阻断` |
| `drama_mapping_ambiguous` | 同一素材对应多条不一致短剧映射 | `阻断` |
| `drama_label_invalid` / `drama_label_unsafe` | 旧版本短剧标签无法核验或含禁发内容 | `阻断`；修正标签 |
| `pool_item_invalid` / `material_safety_check_failed` | 旧版通用素材池/安全校验失败 | `阻断`；结合消息和源数据定位 |

## 短剧集数池

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `drama_not_found` / `drama_id_invalid` | 短剧不存在或 ID 无效 | `阻断` |
| `drama_metadata_ambiguous` / `drama_resource_invalid` | 元数据不唯一或资源数据不完整 | `阻断` |
| `drama_no_free_episodes` | 没有可发布免费集数 | 当前短剧结束，不应重试同一集 |
| `drama_episode_gap` | 免费集数不连续 | `阻断`；修复资源序列 |
| `drama_episode_url_ambiguous` | 同一集存在多个不一致 URL | `阻断` |
| `drama_progress_invalid` | 池内下一集进度与源数据不一致 | `阻断/待核查` |
| `x_post_drama_selection_failed` / `x_post_drama_query_failed` | 短剧选择逻辑或只读查询整体失败 | 本轮零发布；查询恢复后由下一排期再试 |
| `x_post_schedule_drama_shortage` | 本时段没有足够合格短剧候选 | 本轮 `failed_preflight`，未发送 X 请求 |
| `x_post_drama_pool_check_conflict` | 短剧拒绝结果写入时池状态已变化 | `阻断`；重新读取后再处理 |

## 媒体下载与格式预检

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `invalid_media_url` | 下载地址不是合法 HTTPS URL | `阻断` |
| `media_allowlist_not_configured` | 素材域名白名单未配置 | `阻断`；修配置 |
| `media_host_not_allowed` | 素材域名不在白名单 | `阻断`；不得绕过 SSRF 门禁 |
| `http_response_too_large` | 下载响应超过安全读取上限 | `阻断` |
| `media_download_failed` | HTTP/网络/响应中断导致下载失败 | 临时网络失败可在有审计的恢复流程复检；不直接重发已开始的 Post |
| `invalid_media_response` | Content-Length、分片或响应内容无效/为空 | `阻断但可复检` |
| `media_too_large` | 文件超过 512 MiB 或 X 分片上限 | `阻断`；压缩后再用 |
| `media_probe_failed` / `image_probe_failed` | ffprobe/Pillow 无法读取素材 | `阻断但可复检` |
| `invalid_media` | 文件缺失、为空或不是可上传内容 | `阻断` |
| `invalid_media_type` | 下载类型与源类型不一致，或 MIME 不支持 | `阻断但可复检` |
| `invalid_media_codec` | 视频不是 H.264/yuv420p 或音频不是 AAC-LC | 可进入 GPU 修复；修复失败则阻断 |
| `invalid_media_dimensions` | 分辨率或宽高比不符合 X | 可进入 GPU 修复；修复失败则阻断 |
| `invalid_media_scan` | 视频不是逐行扫描 | `阻断但可复检` |
| `invalid_media_duration` | 小于 0.5 秒、普通账号超过 140 秒或会员路径超过 4 小时 | 选会员账号/压缩/换素材 |
| `invalid_media_frame_rate` | 帧率无效或超过 60 fps | `阻断但可复检` |
| `invalid_image` / `invalid_image_dimensions` | 图片格式、签名或尺寸无效 | `阻断` |
| `x_long_video_requires_premium` | 长视频当前没有可用的同语言 Token 实测会员直发/relay 路径 | 本轮跳过并保留素材；有可用 relay 时由会员账号发原 Post、目标账号 Repost，不应写此码 |
| `media_preflight_changed` | 发布时下载到的文件与冻结 SHA/大小/时长不一致 | `阻断`；防止源文件静默替换 |
| `media_preflight_failed` | 旧/兜底媒体预检错误 | 本轮不建队列或不发布，查看具体消息 |

## GPU 修复与 COS

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `media_repair_disabled` | GPU 修复服务被关闭 | `阻断`；恢复服务后复检 |
| `source_not_repairable` / `trigger_not_repairable` | 源文件或触发原因不支持自动修复 | `阻断`；人工换源/转码 |
| `source_too_large` | GPU 下载源超修复服务上限 | `阻断` |
| `profile_mismatch` | 请求的转码 profile 与服务不一致 | `阻断`；同步配置 |
| `repaired_media_invalid` | 修复产物仍不满足 X 合同 | `阻断但可复检`；显式强制重制使用香港 GPU，COS HEAD 与 CPU 二次探测通过后清错 |
| `repaired_media_too_large` / `repaired_media_empty` / `repaired_media_missing` | 修复产物过大、为空或不存在 | `阻断` |
| `repaired_media_duration_invalid` / `repaired_media_duration_too_long` / `repaired_media_duration_mismatch` | 修复后时长不合法、超账号上限或与预期不一致 | `阻断` |
| `x_post_media_repair_invalid_request` | CPU 发往 GPU 的修复请求不合法 | 工程配置/契约错误 |
| `x_post_media_repair_unreachable` | GPU 修复 API 网络不可达 | 临时故障；恢复后走受控复检 |
| `x_post_media_repair_invalid_response` | GPU 返回结构或探针结果不符合合同 | `阻断`；核对 CPU/GPU 版本 |
| `x_post_media_repair_fingerprint_mismatch` | 下载产物 SHA/大小与 GPU 回执不一致 | `阻断`；可能是源漂移/对象覆盖 |
| `x_post_media_repair_probe_mismatch` | CPU 与 GPU 的媒体探针结果不一致 | `阻断` |
| `source_integrity_mismatch` | GPU 实际源文件与请求指纹不一致 | `阻断` |
| `manifest_invalid` / `job_key_conflict` | 修复审计清单无效或同 job key 指向不同任务 | `阻断` |
| `cos_sdk_unavailable` / `cos_upload_failed` / `cos_head_failed` | COS SDK、上传或 HEAD 校验失败 | 临时故障可复检；香港 GPU worker 上传带有限次重试，最终仍须 COS HEAD 和 CPU 指纹复检 |
| `cos_verification_failed` / `cos_object_conflict` | COS 对象大小/SHA 不一致或同 key 内容冲突 | `阻断`；禁止覆盖后假定成功 |
| `storage_error` / `invalid_configuration` | GPU 本地存储或配置错误 | `阻断`；修配置/磁盘 |

## 账号、授权与语言路由

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `x_oauth_not_configured` | X OAuth 客户端未配置 | `阻断` |
| `x_token_missing` | 账号没有可用 Access Token 文件 | `阻断`；重新授权 |
| `x_token_invalid` | X 返回 401 或本地 Token 无效 | `阻断`；重新登录/授权 |
| `x_token_revoked` | Refresh Token/授权已撤销 | `阻断`；重新授权 |
| `x_identity_mismatch` | Token 对应 X 用户与绑定账号不一致 | `阻断`；禁止错号发布 |
| `x_account_not_found` | 后台账号记录不存在 | `阻断` |
| `x_account_disabled` | 账号被后台停用 | `阻断` |
| `x_disconnect_pending` | 账号处于待退出/注销流程 | `阻断` |
| `x_account_publish_not_approved` | 账号未获后台发布批准 | `阻断` |
| `x_account_not_publishable` | 状态、审批、Token 或实时能力不满足发布 | `阻断`；重新校验账号 |
| `x_account_drama_language_invalid` | 账号语言为空或不是支持的语言 | `阻断` |
| `x_account_drama_language_conflict` | 账号还有另一语言的未完成绑定短剧 | `阻断`；先处理原绑定 |
| `x_post_account_language_mismatch` / `x_post_drama_account_language_mismatch` / `x_auto_account_language_mismatch` | 冻结候选语言与账号语言不一致 | 本轮零发布；修正路由 |
| `x_post_premium_relay_unavailable` | 没有同语言、可发布且会员有效的 relay 账号 | 长视频本轮不发布 |
| `x_accounts_unavailable` / `x_posts_unavailable` | 账号 sidecar 或返回合同不可用 | 基础设施错误；恢复后再排期 |

## 文案、短链与本地存储

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `invalid_post_template` | 文案模板为空、过长、占位符/括号无效或使用不支持字段 | `阻断`；修模板 |
| `x_post_copy_too_long` | 固定文案已占满 280 权重，描述无法安全截断 | `阻断`；缩短模板 |
| `invalid_short_base_url` / `invalid_short_link_target` | 短链域名或目标不是允许的 HTTPS 地址 | `阻断` |
| `short_link_conflict` | 同一短链 ID 已指向其他目标 | `阻断`；不得覆盖 |
| `short_link_write_failed` | 短链 HTML 原子写入失败 | 明确失败；修磁盘/权限后受控处理 |
| `x_post_storage_unavailable` | 数据盘、工作目录、SQLite 或挂载身份不可用 | `阻断`；禁止降级到根盘 |
| `x_post_storage_conflict` | 台账、唯一约束、队列/池进度出现不一致 | `阻断/待核查`；不得手改后重发 |

## 排期、队列与防重复

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `x_post_schedule_material_shortage` | 素材池当前没有候选 | 本轮零发布；后续自然排期可再查 |
| `x_post_schedule_material_preflight_shortage` | 候选存在，但没有任何一个通过本轮媒体预检 | `failed_preflight`；修素材/媒体链后复检 |
| `x_post_schedule_candidate_shortage` | 候选数量不符合冻结账号范围 | 本轮零发布 |
| `x_post_schedule_account_mismatch` | 候选账号顺序/范围与冻结配置不一致 | `阻断` |
| `x_post_schedule_material_assignment_invalid` | 素材随机分配不是有效排列 | 工程错误 |
| `x_post_schedule_plan_mismatch` / `x_post_schedule_plan_incomplete` | 返回队列与冻结计划不一致或冻结 run 缺队列 | `阻断/待核查` |
| `x_post_schedule_config_changed` / `x_post_schedule_version_conflict` | 计划期间配置版本发生变化 | 本轮不建队列；按新配置等下一排期 |
| `x_post_schedule_collision` / `x_post_schedule_slot_in_progress` | 同一时段冲突或正在执行 | 不并发重复执行 |
| `x_post_schedule_run_exists` / `x_post_schedule_run_not_found` / `x_post_schedule_not_found` | run 已存在、缺失或时段不存在 | 幂等/状态冲突；重新读取，不盲重试 |
| `x_post_schedule_lease_conflict` | 当前 run 的身份、状态或租约已变化 | 停止当前 runner；重新读取台账，不并发接管 |
| `x_post_schedule_plan_attempt_conflict` | 建计划前的持久化尝试标记与当前 run 不一致 | `阻断/待核查`；不得再次调用建计划接口 |
| `x_post_schedule_plan_unknown` | 已尝试建计划，但无法确认服务端是否落下 queue | 按未知写结果终态化；先查台账，禁止自动重建计划 |
| `x_post_pool_required` / `x_post_pool_item_not_found` | 候选没有绑定素材池或池记录不存在 | `阻断` |
| `x_post_pool_item_occupied` | 素材已被 queue/手动 reservation 占用 | 不重复创建 |
| `x_post_pool_item_published` / `x_post_material_already_used` | 素材已经发布/进入历史队列 | 不重复发布 |
| `x_post_pool_item_unavailable` | 池状态、身份、创建时间或复检证据发生变化 | 重新读取；不沿用旧候选 |
| `x_post_pool_fifo_conflict` | 候选不是当前 FIFO，或“待可投放”没有本轮到点证据 | 本轮零发布；重新 selector |
| `x_post_account_day_already_reserved` | 同账号当天已有发布占位 | 不重复安排 |
| `x_post_idempotency_conflict` | 同幂等键对应不同请求 | `阻断`；核对调用方 |
| `x_post_log_not_found` / `x_post_log_not_prepared` / `x_post_log_conflict` | 发布日志缺失、尚未准备或状态冲突 | `阻断/待核查` |
| `x_post_queue_not_found` / `x_post_state_conflict` | queue 缺失或状态机不允许当前动作 | `阻断/待核查` |
| `x_post_retry_requires_review` | 该日志已执行过，明确禁止自动重复发帖 | 人工确认后走专用恢复，不直接重跑 |
| `x_post_schedule_preflight_failed` | 只读查询/未知预检异常的统一码 | 零 X 写入；修复根因后按审计恢复 |
| `x_post_schedule_preflight_interrupted` | 预检进程被中断 | 仅 0 queue/0 log/0 unknown 时允许受控恢复 |
| `x_post_schedule_operator_deferred_for_due_slot` | 运营恢复为了保护即将到期的正常时段而主动零写让路 | 不是素材错误；按既有 corrective 审计链恢复 |
| `x_post_bound_drama_failed_media_recovery_conflict` | 绑定短剧失败媒体恢复的完整队列、绑定或审计证据不一致 | `阻断`；修正清单或现场漂移后重新 validate-only，不部分恢复 |
| `x_post_bound_drama_manifest_invalid` | 短剧恢复清单格式、范围或身份无效 | `阻断`；修正精确清单，不猜测或部分执行 |
| `x_post_bound_drama_episode_unavailable` | 清单中的绑定短剧集数已无法从来源精确读取 | `阻断`；重新只读核对来源与绑定漂移 |
| `x_post_bound_drama_repair_proof_invalid` | GPU 修复结果缺少匹配的 job/source/media 证据 | `阻断`；不应用恢复、不触发 X |

## 手动发布与自动模板

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `x_post_manual_source_preflight_failed` | 所选 exact-ID 素材没有全部通过源数据检查 | 整批 0 queue/0 X 写入 |
| `x_post_manual_media_preflight_failed` | 所选素材没有全部通过媒体检查 | 整批 0 queue/0 X 写入 |
| `x_post_manual_account_mismatch` | 冻结账号与实时校验账号不一致 | `阻断` |
| `x_post_manual_material_mismatch` | 预检产物不是冻结素材集合 | `阻断` |
| `x_post_manual_candidate_shortage` / `x_post_manual_scope_mismatch` | 素材数、账号数或范围不一致 | `阻断` |
| `x_post_manual_material_unavailable` | 素材已占用、已使用或不满足手动发布 | `阻断` |
| `x_post_manual_plan_exists` / `x_post_manual_run_terminal` | 同请求已有计划或已终态 | 不重复创建/执行 |
| `x_post_manual_run_not_found` / `x_post_manual_source_mismatch` | 手动 run 缺失或来源不符 | `阻断` |
| `x_post_manual_invalid_response` / `x_post_manual_failed` | runner/sidecar 返回合同无效或兜底失败 | 工程错误；先查日志 |
| `x_post_auto_template_duration_exceeded` | 自动模板素材超过 600 秒 | `阻断` |
| `x_post_auto_template_material_unavailable` | 自动模板指定素材不可用 | `阻断` |
| `x_post_auto_template_scope_mismatch` / `x_post_auto_template_idempotency_conflict` | 自动模板范围或幂等身份冲突 | `阻断` |

## 配置、补发、池管理与受控恢复

这些码主要面向管理员或恢复脚本，不应直接当成“换一个素材再发”的理由。

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `invalid_schedule_mode` / `invalid_random_daily_count` | 排期模式或每日随机次数配置无效 | 修配置；本轮不发布 |
| `x_post_random_plan_generation_failed` / `x_post_random_times_must_be_empty` | 随机排期无法生成或固定/随机字段互相冲突 | 修配置并生成新版本，不改旧冻结计划 |
| `x_post_schedule_clock_skew` | runner 日期/时区与北京时间不一致 | `阻断`；先校时 |
| `x_post_schedule_failure_scope_mismatch` | 失败审计的账号/时段范围与冻结 run 不一致 | `阻断`；不得写入错误范围 |
| `x_post_previous_day_runner_date_conflict` / `x_post_previous_day_runner_not_allowed` | 昨日补偿日期不符或当前版本不允许执行 | `阻断` |
| `x_account_owned_by_other` / `x_admin_required` | 当前后台用户不是账号所有者或缺少管理员权限 | 权限错误，不触发发布 |
| `x_disconnect_failed` | 清理历史授权凭证失败 | 保持停用/失败关闭，修凭证存储 |
| `x_post_account_mismatch` | queue 冻结账号与调用账号不一致 | `阻断`；防错号 |
| `x_daily_scope_invalid` / `x_daily_account_scope_denied` | 每日/补发账号不在冻结配置范围 | `阻断` |
| `x_daily_plan_invalid_request` / `x_daily_plan_invalid_response` / `x_daily_plan_query_invalid_response` / `x_daily_failure_invalid_response` | 每日计划请求、查询或失败审计合同无效 | 工程错误；本轮失败关闭 |
| `x_post_daily_candidate_shortage` / `x_post_daily_run_exists` | 每日候选不足或相同日期 run 已存在 | 不创建重复批次 |
| `x_post_daily_pool_shortage` / `x_post_daily_candidate_preflight_shortage` | 每日素材池数量不足，或候选均未通过预检 | 本批 0 X 写入；修素材后再走排期 |
| `x_post_daily_copy_validation_failed` / `x_post_daily_account_mismatch` | 每日文案/候选校验失败，或账号顺序与冻结范围不符 | `阻断` |
| `x_post_daily_resume_conflict` | 恢复每日 run 时现有 queue/log 与冻结计划不一致 | 待核查，不直接续跑 |
| `x_catchup_parent_not_found` / `x_catchup_parent_not_completed` / `x_catchup_parent_mismatch` | 补发父批次缺失、未完全成功或身份不符 | 不允许补发 |
| `x_catchup_no_missing_accounts` / `x_catchup_reason_denied` | 没有缺失账号或补发理由不在白名单 | 零写 no-op/拒绝 |
| `x_post_catchup_candidate_shortage` / `x_post_catchup_scope_mismatch` | 补发候选不足或范围不符 | `阻断` |
| `x_post_catchup_parent_not_ready` / `x_post_catchup_run_exists` / `x_post_catchup_run_not_found` | 补发父状态、幂等 run 或目标 run 不满足条件 | 重新读取，不盲重试 |
| `x_post_drama_pool_required` / `x_post_drama_pool_item_not_found` / `x_post_drama_pool_item_exists` | 短剧候选未绑定池、池记录缺失或重复 | `阻断` |
| `x_post_drama_pool_item_bound` / `x_post_drama_pool_item_occupied` | 短剧已经绑定账号或被 queue 占用 | 不重复分配 |
| `x_post_drama_pool_item_unavailable` / `x_post_drama_pool_needs_review` | 短剧池状态不可用或存在未知发布结果 | 待核查，不自动推进集数 |
| `x_post_drama_already_used` / `x_post_drama_episode_already_used` | 短剧或该集已有历史发布身份 | 不重复发布 |
| `x_post_drama_owner_not_configured` | 已绑定短剧的账号不在当前配置范围 | 修账号配置，不改绑绕过 |
| `x_post_drama_assignment_conflict` / `x_post_drama_account_binding_conflict` | 事务期间短剧被另一账号绑定或绑定状态漂移 | 重新读取，不重放旧计划 |
| `x_post_drama_pool_revalidation_conflict` | 短剧复检时状态/集数/错误码已变化 | 重新 selector |
| `x_post_drama_priority_conflict` | 当前短剧状态不允许设/取消高优 | 管理操作失败，不触发发布 |
| `x_post_drama_replay_not_eligible` / `x_post_drama_replay_queue_in_progress` / `x_post_drama_replay_run_in_progress` | 全量重播条件不满足、仍有 queue 或 run | 不允许重播 |
| `x_post_drama_replay_history_conflict` / `x_post_drama_replay_snapshot_conflict` | 重播历史或冻结快照不一致 | `阻断`；不得改账本绕过 |
| `x_post_drama_scope_compensation_not_allowed` / `x_post_drama_scope_compensation_conflict` | 短剧范围补偿不满足门禁或审计冲突 | 零写拒绝 |
| `x_post_failed_preflight_recovery_not_allowed` / `x_post_failed_preflight_recovery_conflict` | 预检失败不在允许恢复原因内，或现场已漂移 | 不恢复 |
| `x_post_failed_media_recovery_not_allowed` / `x_post_failed_media_recovery_conflict` | 媒体失败缺少精确 commit/指纹或现场冲突 | 不恢复 |
| `x_post_material_operator_stop_recovery_not_allowed` / `x_post_material_operator_stop_recovery_conflict` | 运营停止任务不满足零写恢复条件或范围冲突 | 不恢复 |
| `x_post_pre_x_recovery_not_allowed` / `x_post_pre_x_recovery_conflict` | 只允许已证明“尚未调用 X”的失败恢复；否则拒绝 | 不恢复 |
| `x_post_previous_day_recovery_not_allowed` / `x_post_previous_day_recovery_conflict` | 昨日恢复缺少批准理由/commit 或现场漂移 | 不恢复 |
| `x_post_premium_relay_accounts_invalid_response` | 可用 relay 账号查询返回合同无效 | 本轮长视频不发布 |
| `x_post_relay_binding_conflict` / `x_post_relay_reassignment_fenced` | relay 绑定冲突，或原 Post 开始后禁止换 relay | 停止并核查 |
| `x_post_repost_ledger_not_found` | Repost 审计账本缺失 | `阻断/待核查` |
| `x_post_auto_template_invalid_response` / `x_post_auto_template_recovery_fenced` | 自动模板响应无效或已被规范恢复流程封锁 | 不重复执行 |
| `x_post_manual_pool_forbidden` | 手动发布请求试图使用不允许的池/来源 | `阻断` |
| `x_post_run_not_found` | 目标发布 run 不存在 | 重新读取任务 ID，不创建替代事实 |

## X 上游、上传、Post 与 Repost

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `x_post_rate_limited` | X 返回 429、用量上限或 rate-limit payload | `明确失败`；停止本批后续发布，等窗口恢复；不可立刻重跑 |
| `x_upstream_error` | X 明确拒绝或上传阶段网络失败；包括 400/403、账号临时锁定、权限/能力限制、返回结构错误等 | `明确失败`；按已脱敏上游详情处理，不自动重发 |
| `x_media_processing_failed` | X 媒体异步处理明确失败 | `明确失败`；检查媒体后换源/转码 |
| `x_media_processing_timeout` | X 媒体在轮询上限内未完成 | 发布 Post 尚未开始；人工确认媒体状态后处理 |
| `x_post_outcome_unknown` | Create Post 请求可能已到 X，但响应/ID无法确认 | `结果未知`；冻结后续自动发布，先查账号时间线和 readback |
| `x_repost_outcome_unknown` | Repost 可能已成功，但响应无法确认 | `结果未知`；先查目标账号，禁止自动 Repost |
| `x_publish_unknown` | sidecar 对上述未知写结果的公网/runner 统一码 | `结果未知`；禁止自动重试 |
| `x_post_unknown_outcome` | 台账发现历史 unknown 或 post/repost creating | `结果未知`；阻断同账号后续自动发布 |
| `x_post_internal_error` | X 写入前后的本地未分类异常，且当前证据显示结果已知 | `明确失败`；查服务日志和台账 |
| `x_post_repost_state_conflict` | relay 原 Post 未就绪或 Repost 状态不允许当前动作 | `阻断/待核查` |
| `x_post_premium_relay_unavailable` | relay 会员账号在冻结后失去资格 | 原 Post/Repost 按台账状态停止，不改绑绕过 |

截图中的 `x_upstream_error / HTTP 403 / account is temporarily locked` 属于“X 明确拒绝”，
与本次 `drama_not_yet_deliverable` 无关。应先在 X 解锁该账号并重新核验 Token/发布资格，
不能通过自动重试来验证。

## 服务间合同与兜底错误

| 错误码 | 中文含义 | 动作/是否重试 |
| --- | --- | --- |
| `x_post_account_needs_review` | 该账号有未完成或结果待核对的发布；也可能是冻结转发源被占用 | 新批次跳过该账号；原未知记录不变，不可通过清状态或重发确认 |
| `x_post_account_locked` | X 明确返回账号临时锁定 | 新批次跳过该账号；先登录 X 解锁并人工核对。资料接口成功不代表解除发布锁定 |
| `x_post_schedule_account_state_changed` | 素材容量预检后账号可用范围发生变化 | 本次建计划为已知拒绝、零队列；不得用过期容量证据继续建计划 |
| `x_post_media_repair_config_invalid` | 短剧逐条修复配置缺失、不安全或不符合现行协议 | 尚未尝试发帖；修正服务配置，不能放宽媒体校验 |
| `x_post_media_preparation_failed` | 短剧上传前的媒体准备出现未分类异常 | 保留原队列并记零次发帖尝试；排查后按精确恢复流程处理 |
| `x_post_bound_drama_source_changed` | 历史短剧恢复时权威素材 ID 或 URL 与原冻结队列不一致 | 阻断恢复，不修复替代源、不改绑定，不自动重发 |
| `x_sidecar_unreachable` / `x_accounts_unavailable` / `x_posts_unavailable` | loopback sidecar 不可达或服务不可用 | 零 X 写入时可在服务恢复后自然再试 |
| `x_sidecar_invalid_response` / `x_account_invalid_response` / `x_material_keys_invalid_response` | sidecar 返回结构不符合合同 | 工程错误，失败关闭 |
| `x_post_pool_invalid_response` / `x_post_pool_check_invalid_response` | 素材池查询/校验响应无效 | 本轮零发布 |
| `x_post_drama_pool_invalid_response` / `x_post_drama_pool_check_invalid_response` | 短剧池查询/校验响应无效 | 本轮零发布 |
| `x_post_storage_preflight_invalid_response` | 数据盘预检响应无效 | `阻断` |
| `x_post_schedule_invalid_response` / `x_post_schedule_plan_invalid_response` / `x_post_schedule_failure_invalid_response` | 排期 claim/plan/failure 审计响应无效 | `阻断/待核查` |
| `x_post_schedule_heartbeat_invalid_response` | run 租约心跳响应不符合冻结身份合同 | 停止当前 runner；不得在租约未知时继续建计划或发布 |
| `x_post_bound_drama_recovery_store_failed` | 历史短剧媒体恢复数据库事务失败且已完整回滚 | 阻断；禁止自动重试，先核对恢复 audit、queue/log、剧集绑定及 relay ledger |
| `x_publish_invalid_response` | 发布接口响应与 queue/log 合同不一致 | 按可能未知结果处理，先查台账/X |
| `unexpected_error` | runner 未分类异常 | 失败关闭；不得凭此自动重发 |

## 部署前生产只读快照（2026-08-25 15:33，北京时间）

未发布素材池 `last_error_code`：

| 错误码 | 数量 |
| --- | ---: |
| 空（无历史校验错误） | 347 |
| `x_long_video_requires_premium` | 98 |
| `material_language_not_scheduled` | 73 |
| `material_source_tag_unsafe` | 38 |
| `repaired_media_invalid` | 6 |
| `drama_not_yet_deliverable` | 5 |
| `material_has_violation` | 3 |
| `cos_upload_failed` | 2 |
| `material_duration_missing` | 2 |
| `material_not_found` | 1 |

5 条 `drama_not_yet_deliverable` 均仍为 `status=unpublished`，最后校验时间停留在
2026-08-21，而权威开放时间为 2026-08-22 00:00（北京时间）。这证明原故障是候选查询
把临时错误永久排除，未发生重新校验；不是已建 queue 或 X 上游发布失败。

## 部署后生产验收快照（2026-08-25 18:22，北京时间）

- 六个指定历史/修复错误码数量均为 0：`x_long_video_requires_premium`、
  `material_language_not_scheduled`、`material_source_tag_unsafe`、
  `repaired_media_invalid`、`material_has_violation`、`cos_upload_failed`。
- 8 条媒体素材均在香港 GPU 强制重制并通过 COS HEAD 与 CPU 二次复检。
- 其余 3 条确定无效、未发布、无队列/活动占用的素材已精确删除。
- 唯一剩余非空错误为 5 条 `drama_not_yet_deliverable`；它们继续显示“待可投放”，
  由后续自然素材排期按当前权威时间重新校验，不作为永久不可用状态。
- 素材池/队列/日志/未知结果为 `841/627/627/0`，SQLite integrity 为 `ok`，
  foreign-key violations 为 0；本次验收没有创建真实 Post/Repost。

## 代码来源

### 2026-08-28 下载完整性补充

| 错误码 | 中文含义 | 处理边界 |
| --- | --- | --- |
| `media_download_incomplete` | 素材下载不完整，连续三次读取仍被截断 | 仅重读同一素材URL；未进入X写入，不自动重发帖子 |
| `media_download_length_mismatch` | 素材响应长度超过声明长度 | 拒绝该响应，不拼接、不换源、不覆盖已验证文件 |

- `features/x_posts/selector.py`：素材/短剧映射与 `deploy_time`。
- `features/x_posts/drama_selector.py`：短剧集数池。
- `features/x_posts/service.py`：池/queue/log、媒体门禁、X 上传/Post/Repost。
- `features/x_posts/media_repair.py`：GPU 修复与 COS。
- `features/x_accounts/oauth_service.py`：账号、Token、发布资格。
- `scripts/x_post_daily_runner.py`、`scripts/x_post_schedule_runner.py`、
  `scripts/x_post_manual_runner.py`：排期/手动流程与服务间合同。

该清单不回显 Token、请求头、完整上游响应或堆栈；未知内部码在运营页面统一显示
“发布前检查失败，请联系技术人员”，结构化码仅保留在后台审计中。
