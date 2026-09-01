# 测试用例

## 测试范围

schema/store/resolver、media repair、Sidecar call-order、fixed/random scheduler、waiting retry、DTO/UI、历史 141 与非短剧回归。

## 测试数据

- 时长：`139.999`、`140.000`、`140.000001`、`141`、长视频。
- 账号：目标 Premium/非 Premium/升级、同语言与跨语言 relay、private/inactive/unapproved/blocked。
- 媒体：无需修复、standard/premium 修复、修复后跨边界、URL/SHA/size/width/height/duration 漂移。
- 状态：pending、waiting、resolved、重复请求、事务崩溃、Post/Repost unknown、历史 exact 141。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| DR-001 | 新 drama 计划 | feature on，fixed/random | 创建自然计划 | pending；零媒体调用、零 relay | P0 | 已通过（mock） |
| DR-002 | 短片边界 | final 139.999/140 | 解析路线 | 目标 direct | P0 | 已通过（mock） |
| DR-003 | 长片目标会员 | final >140，目标 eligible/public | 解析路线 | 目标 direct | P0 | 已通过（mock） |
| DR-004 | 长片 relay | 目标非会员，多同语言候选 | 解析路线 | lifetime load 最小、ID 稳定；ledger 同事务 | P0 | 已通过（mock/SQLite） |
| DR-005 | 无 relay | final >140，目标非会员 | 解析并自然重查 | waiting；零 log/ledger/进度 | P0 | 已通过（mock/SQLite） |
| DR-006 | 目标升级 | waiting 后目标 eligible | 下个自然周期 | 使用冻结媒体解析 direct | P0 | 已通过（mock/SQLite） |
| DR-007 | 修复策略 | 原片短/长且需修复 | prepare | standard/premium 正确，最终时长定路 | P0 | 已通过（mock） |
| DR-008 | 一次下载复用 | pending 首次解析并发布 | 统计 downloader/upload | 源下载一次，同一本地文件上传 | P0 | 已通过（mock） |
| DR-009 | 漂移与崩溃 | 已冻结 evidence/事务注入 | 重入 | X write 前拒绝或原子回滚 | P0 | 已通过（mock/DB trigger） |
| DR-010 | 历史 141 | 无 companion 历史审计队列 | 执行恢复回归 | 旧特例不变且不扩权 | P0 | 已通过（历史回归） |
| DR-011 | 非短剧流程 | material/manual/auto/daily/catchup | 全量回归 | 不产生 pending/waiting | P1 | 已通过（全量 X） |
| DR-012 | UI/API | pending/waiting/resolved DTO | 打开日志/剧集页 | 中文路线；0/null=待检测 | P1 | 已通过（contract） |

生产自然排期中的首条短 direct 与首条长 relay 属于上线后业务验收，不以测试帖子代替，当前不标记完成。

## 回归范围

`test_x_post_schedule_runner`、`test_x_post_multi_schedule_store`、`test_x_posts`、`test_x_accounts`、`test_x_post_premium_relay_repost`、历史 bound-drama recovery、material/manual/auto UI contract 与完整 unittest discovery。所有 X client 写操作必须为 mock。
