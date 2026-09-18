# API 文档

## 模板保存契约

沿用既有新建/更新模板接口，不新增公开端点。请求的 `language` 从必填改为可选兼容字段；新页面不发送该字段，服务端规范化后的新版本配置不保存该字段。其余必填项、产品映射、指标、排序、发布计划和视频模板规则不变。

旧版本读取响应可能仍含 `config.language`，客户端和计划器不得以它选择素材；编辑后保存的新版本不含该字段。

## 内部选择契约

- `page_language(value)`：返回规范化、通过既有格式校验的语言，或空字符串。
- `candidate_snapshots_for_pages(config, pages, materials)`：按唯一 Page 语言建立 `language -> CandidateSnapshot`，保留指标代次与日期。
- Page 选择顺序沿用既有权限、未知结果、Token 和冷却保护；语言无效时不选材。
- `_candidate_limit` 是素材仓库的计划器内部参数，公开模板接口必须拒绝；它不进入保存配置。

## 素材 URL 规范化契约

历史 HTTP URL 仅在解析后的 hostname 精确匹配 `advertising-1306474899.cos.ap-hongkong.myqcloud.com`，且不带 userinfo、端口或 fragment 时，可将 scheme 改为 HTTPS。对象路径、百分号编码与 query 内容保持；后续请求直接使用 HTTPS，不回写源 MySQL 地址。

其他 HTTP 主机及伪装后缀主机继续拒绝；合法 HTTPS 的原校验规则保持不变。这是针对已验证自有 bucket 的窄范围兼容，不是对任意 HTTP 地址的自动升级。BUG-002 修复 `f7cb58f` 已部署，TL 恢复 500 候选，最新 192 项全套通过。

## 未来运行扩充接口

`extend_future_runs(store, run_ids, pages, materials, operation_id=..., expected_scope=..., expected_version=..., max_pages=200, max_daily_jobs=1000, publish_floor='')`：只追加准确运行内缺少的 Page；调用方负责实时 Page/旧队列/全局容量复查、备份和预演。范围使用 `scope_fingerprint(pages)` 固定 Page、组、规范化语言与 Token 资格数；运行须为同配置、启用版本的未来自动运行，至少留出十分钟。

单事务覆盖全部运行、Page 快照、任务、计数与 `fb_auto_pool_extension` 回执；任一运行缺素材或存在未知结果/同 Page 同时隙冲突时整体回滚。`publish_floor` 只调整新增任务的计划时间，已有任务保持原值。重复操作依据回执幂等返回。

操作 CLI `scripts/fb_extend_future_pool.py` 另依赖持续维护窗口：自动写入者暂停、活动与未确认发布归零、候选 JSON 和 metadata 冻结并记录 SHA，从副本预演至 apply 后读回不能恢复自动写入。CLI 的预览绑定读回发生在提交后，不提供无需暂停的在线执行保证。

## 原因码

| 原因码 | 含义 | 行为 |
| --- | --- | --- |
| `fb_auto_page_language_missing` | Page 语言为空或格式无效 | 该 Page skipped，无素材任务 |
| `fb_auto_no_eligible_video` | 该语言无满足规则和冷却要求的视频 | 该 Page skipped，不跨语言回退 |
| `fb_auto_page_language_changed` | 第二次 Page 读取出现未建立候选快照的新语言 | 整次规划延后，零部分落账 |
| `fb_page_missing_eligible_token` | Page 无可用资格 Token | 继续沿用原跳过逻辑 |

Page 语言通过原 `fb_auto_run_page.language` 字段冻结；新 run 的指标代次是各语言候选快照代次的去重集合。扩充执行时按需创建独立回执表，新增候选代次写入回执；既有运行的配置与原代次保持不变。
