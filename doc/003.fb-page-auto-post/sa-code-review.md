# SA 代码评审

## 结论

本地候选通过；禁止直接上线。指标 SQL 修正版的生产只读 EXPLAIN 与 Graph 既有视频 status 只读 canary 已完成；仍须 GitHub review、GPU/COS/NVENC 集成和 live gate=0 部署。

## 评审范围

FB package、runner、main API、navigation/UI、units/env、docs/tests。

## 问题清单

| 编号 | 严重级别 | 文件/位置 | 问题 | 建议 | 状态 |
| --- | --- | --- | --- | --- | --- |
| CR-01 | P0 | `core.py` | SQLite context 未关闭 | closing connection | 已修复，BUG-001 |
| CR-02 | P0 | `publisher.py` | Graph ID误作published | submitted+reconcile | 已修复 |
| CR-03 | P0 | `repositories.py` | Page级重复指标查询 | 每运行一次候选快照 | 已修复 |
| CR-04 | P1 | nav/app | 权限枚举缺失 | 增加fb_page_posts默认false | 已修复 |
| CR-05 | P1 | core/service | 跨组重叠绕过组独占 | Page联集+唯一索引 | 已修复 |
| CR-06 | P1 | service.tick | closed gate仍冻结任务 | gate关闭只安全清理 | 已修复 |
| CR-07 | P1 | core.update | enabled编辑绕过启用校验 | 强制先停用再编辑 | 已修复 |
| CR-08 | P1 | core.create_run | 启用后成员漂移可形成跨模板同Page | 每次冻结前重查全部启用模板Page交集 | 已修复 |
| CR-09 | P1 | core.schedule_times | 24次完整日窗口拒绝采样稳定失败 | 可行解计数DP随机构造 | 已修复 |
| CR-10 | P1 | publisher.reconcile | 单Token对账及凭证失败永久submitted | 随机不放回轮换，全部明确拒绝转unknown | 已修复 |
| CR-11 | P1 | service/runner/unit | 重入tick重复冻结且5+模板超时 | single-flight；scheduler与plan/prepare/Graph分层 | 已修复 |
| CR-12 | P1 | repositories.material | drama sort未使用且先按s.id截断 | source主键keyset完整扫描；READY指标全量筛选后drama主/material次排序 | 已修复 |
| CR-13 | P2 | repositories.page | 未来group type会误显示AD | 两处显式限定type 0/1 | 已修复 |
| CR-14 | P2 | core.ledger | 对账把发布明确失败次数归零 | upsert取历史与新值最大值 | 已修复 |
| CR-15 | P0 | repositories.material | 真实窗口SQL超时 | 删除insight窗口双扫，改用READY SQLite代次 | 已修复 |
| CR-16 | P0 | scheduler | 到发布时间才冻结/GPU，长tick漏时隙 | future due-slot/watermark/plan/prepare分层 | 已修复 |
| CR-17 | P0 | GPU/Graph | 可能回退源素材 | strict random_overlay响应，Graph只认prepared URL | 已修复 |
| CR-18 | P1 | activation | Page×频率可超过吞吐 | env容量门禁与UI估算 | 已修复 |
| CR-19 | P1 | product | 原通用素材源不适用Dramawave | 服务端锁死1479/6/Dramawave/0 | 已修复 |
| CR-20 | P0 | core.create_run | disable/version/Page增长与全局同槽容量竞态 | catalog后重读Page/冲突/容量，最终事务重验enabled fingerprint | 已修复 |
| CR-21 | P0 | core.due/task | future版本/时隙互斥过宽且submitted会触发唯一冲突 | versioned slot；未来Page任务并存；claim显式跳过执行闭锁 | 已修复 |
| CR-22 | P0 | core.metric | active pointer可被旧代重试回退，且长写阻塞运行库 | refreshed_at单调激活；独立metric DB且同路径启动失败 | 已修复 |
| CR-23 | P0 | repositories.material | drama JOIN生产存在大量重复且单页约40秒 | source FORCE PRIMARY + drama EXISTS(ac) + 确定性元数据 | 已修复 |
| CR-24 | P0 | core/gpu | submitted/prepare失败会同轮重复领取 | 持久next_reconcile/next_prepare，至少5分钟退避 | 已修复 |
| CR-25 | P0 | publisher/core | Graph accepted 与task/ledger分事务 | 单事务提交attempt+Graph ID+task+ledger | 已修复 |
| CR-26 | P1 | main API | run-now超时后写结果不可追踪 | operation_id幂等manual due-slot，主API 202快速返回 | 已修复 |
| CR-27 | P1 | validation/UI | 默认english与生产code不匹配 | english→en，受限BCP47含zh-tw，UI code下拉 | 已修复 |
| CR-28 | P1 | GPU work root | 失败job可长期堆积至磁盘满 | 严格根目录/名字、保留期和有界清理，不跟随链接 | 已修复 |
| CR-29 | P0 | core.create_run | ahead future时隙可并发选中同Page同素材 | active预留纳入冷却；事务内重查、改选并插task | 已修复 |
| CR-30 | P0 | runner/unit | 8 Token最坏960秒超过600秒HTTP及短对账租约 | execute/reconcile 1200租约、1300 HTTP、1500 unit，每轮4任务 | 已修复 |

## 编译 / 验证结果

最终编译、单元/契约回归及静态/敏感扫描见 `test-report.md`。
