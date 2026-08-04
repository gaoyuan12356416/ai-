# 测试用例

## 测试范围

语言规范化、旧库迁移、个号单条/批量设置、素材无匹配等待、自动跨池语言 FIFO、并发原子性、账号后改、queue 恢复、手动测试边界、预制作页面及 TT Post 全量回归。

## 测试数据

- 临时 SQLite：旧库无 `drama_language` 列、空/非空素材语言、多个语言池和历史 queue/run。
- 假账号：`101=en`、`102=es`、`103=EN`、`104=pt-br`，不同 `is_aigc`。
- 假素材：按 `created_at,id` 交错的 `en`、`es`、`pt-br` 和历史空语言。
- 假 Creator Info、Token、GPU 和 TikTok 响应；禁止真实网络发布。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| T01 | 旧库增量迁移 | 账号设置表和 recurring pool 无新列 | 连续初始化两次 | 账号默认 `en`、素材路由键回填、复合索引正确、旧行/队列不变、integrity ok | P0 | 通过 |
| T02 | 新库建表 | 空数据库 | 初始化存储 | `drama_language NOT NULL DEFAULT 'en'` | P0 | 通过 |
| T03 | 语言规范化 | 输入 ` PT_BR `、`EN`、`zh-Hans` | 单条保存 | 回显 `pt-br`、`en`、`zh-hans` | P0 | 通过 |
| T04 | Unicode 标签 | 输入合法中文分段 | 保存 | 分段字母数字校验通过并 casefold | P1 | 通过（定向 6 项） |
| T05 | 空/缺失语言 | 新旧请求不带值或传空 | 保存 | 统一保存 `en` | P0 | 通过 |
| T06 | 非法语言 | 连续连字符、特殊符号、超 32 字符 | 保存 | 400，版本和旧值不变 | P0 | 通过 |
| T07 | 单账号乐观锁 | 版本过期 | 保存语言 | 409，不覆盖新版本 | P0 | 通过 |
| T08 | 批量语言保存 | 两账号有效版本 | 批量设置 `es` | 两账号原子保存并回显 `es` | P0 | 通过 |
| T09 | 批量冲突 | 其中一账号版本过期 | 批量保存 | 整批回滚 | P0 | 通过 |
| T10 | 素材语言来源 | fake resolver 返回 `es` | preview | 响应为服务端 `material_language=es` | P0 | 通过 |
| T11 | 无同语言账号入池 | 仅 en 账号，素材 es | 入池并执行预制作 | intake/recurring 成功，最终 `available`，无 queue | P0 | 通过 |
| T12 | 自动跨池领取 | es 素材原 account_id 为 101，账号 102=es 到期 | 自动 claim | 102 领取该素材，pool/run 账号均为 102 | P0 | 通过 |
| T13 | 同语言 FIFO | 三条 en 素材时间不同 | 连续三个 en 账号/时间槽领取 | 严格按 `created_at,id` | P0 | 通过 |
| T14 | 多语言独立 FIFO | 最老 es 无账号，后续 en 有账号 | en 自动 claim | 领取最老 en；es 仍 available | P0 | 通过 |
| T15 | 禁止 fallback | pt-br 账号、仅 pt/en 素材 | 自动 claim | 池空安全跳过，不取 pt/en | P0 | 通过 |
| T16 | 旧空素材 | `material_language=''` | en 与 es 账号分别领取 | 仅 en 可领取 | P0 | 通过 |
| T17 | 同语言并发 | 两个 en 账号同时到期，一条 en 素材 | 两连接并发 claim | 仅一个 run/pool reservation | P0 | 通过 |
| T18 | 并发多素材 | 两个 en 账号、两条 en 素材 | 并发 claim | 两账号领取不同素材，顺序稳定 | P0 | 通过 |
| T19 | 账号语言后改 | 素材未领取，账号 en 改 es | 下一自动时间槽 | 只按 es 领取 | P0 | 通过 |
| T20 | 目标 is_aigc | 入池预分配账号与实际领取账号设置不同 | 自动 claim/冻 queue | pool/queue 使用实际账号当前值 | P0 | 通过 |
| T21 | 已生成 run 恢复 | run 已绑定 pool 后账号改语言 | 恢复 | 使用原 pool/run，不重选 | P0 | 通过 |
| T22 | 已生成 queue | queue 冻结后账号改语言 | runner claim/publish fake | 仍处理原 queue，不生成第二 queue | P0 | 通过 |
| T23 | publish_id/unknown | 已有远端 ID 或结果未知 | 重试 | 只核对，禁止重选/重新初始化 | P0 | 通过 |
| T24 | 手动立即发布 | 明确账号 101、其分池最老素材 | run-now | 不跨池按语言换到 102 | P0 | 通过 |
| T25 | 精确 canary | 配置精确账号/pool | run-now | 只接受精确目标，其他素材不替代 | P0 | 通过 |
| T26 | 账号页 UI | 单条/批量模式 | 回填、编辑、保存、刷新 | 默认 en，payload/列表/回填一致 | P1 | 通过 12/12 |
| T27 | 预制作表 UI | 空语言、已领取和未领取素材 | 渲染 | 剧语言列；空显示 en；未领取显示等待文案；colspan=9 | P1 | 自动化通过 36/36；部署视觉待验收 |
| T28 | 自动配置/随机计划回归 | 现有固定与随机配置 | 保存并生成计划 | 时间、账号计划和版本语义不变 | P0 | 通过 |
| T29 | 品牌披露回归 | 自动发布 | 冻结 queue | 两个品牌披露仍为 false | P0 | 通过 |
| T30 | 预制作账号能力边界 | Creator Info/账号源故障 | 素材入池及预制作 | 仍可完成，不调用实时能力 | P0 | 通过 |
| T31 | 代理审计 | 单条/批量保存语言 | 检查审计详情 | 含规范化语言，无 Token | P1 | 通过 13/13 |
| T32 | 无真实发布 | 全部测试 | 检查 fake/调用记录 | TikTok Create Post 调用为 0 | P0 | 通过 |
| T33 | active canary 隔离 | 101 精确 canary；102 同语言自动到期 | 执行自动 due 后再 run-now | 自动任务不抢 canary pool；101 仍可精确领取；若 101 排期启用则按钮与 run-now 均锁定 | P0 | 通过 |
| T34 | 手动 readiness 精确性 | 仅其他账号分片存在同语言素材 | 查询 schedule 后 run-now | 自动可用数为 1，手动可用数为 0，按钮不可用且 run-now 返回精确池空 | P1 | 通过 |
| T35 | 非法历史语言隔离 | 旧 recurring 行为 `en us` 且无路由列 | 初始化并加入合法 en 素材 | 坏行显示但不匹配；en 账号领取合法 FIFO；查询使用复合索引 | P0 | 通过 |
| T36 | 大历史池账号筛选 | 目标账号记录位于全局第 1001 条 | 按账号及默认条件查询素材池 | 分批读取、每批至多 1000 个素材聚合发布状态；total、分页和目标行准确 | P1 | 通过 |

## 回归范围

- `scripts.test_tt_account_settings_ui`
- `scripts.test_tt_gpu_worker`
- `scripts.test_tt_posts_app_contract`
- `scripts.test_tt_posts_core`
- `scripts.test_tt_posts_service`
- `scripts.test_tt_post_direct_config_core`
- `scripts.test_tt_post_links`
- `scripts.test_tt_post_pool_ui`
- `scripts.test_tt_post_prepare_runner`
- `scripts/test_tt_drama_bridge.js`
- 100% 缩放下个号管理和 TT Post 发布池桌面布局。

## 执行说明

- P0/P1 自动化功能用例均使用临时 SQLite 与 fake Creator/GPU/TikTok；未创建真实 Post。
- 完整 Python 回归、Bridge 断言和静态检查结果见 `test-report.md`。
- T27 的 DOM/文案/列数契约已通过；100% 缩放的登录态浏览器目视确认保留为部署验收项，不以单元测试替代。
