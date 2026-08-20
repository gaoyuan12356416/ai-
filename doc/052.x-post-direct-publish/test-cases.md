# 测试用例

## 测试范围

候选规划、queue 模式隔离、数据库迁移、素材/短剧 Relay、实际发布、失败继续、unknown/429 fence、恢复幂等及既有 X 回归。

## 测试数据

全部使用临时 SQLite、mock MySQL/Sidecar/X HTTP/下载器/prober；生产只做只读前置与真实已认领批次的自然验收。

## 用例列表

| 编号 | 场景 | 前置条件 | 步骤 | 预期结果 | 优先级 | 状态 |
| --- | --- | --- | --- | --- | --- | --- |
| TC-001 | 素材零预检建队 | downloader/prober/repair 调用即抛错 | 执行 material schedule | 不调用三者，创建 deferred queue | P0 | 通过 |
| TC-002 | 短剧零预检建队 | 同上 | 执行 drama schedule | 不调用三者，创建 deferred queue | P0 | 通过 |
| TC-003 | 素材短视频 direct | 源时长 100s | 轻量规划 | direct、duration hint 100 | P0 | 通过 |
| TC-004 | 素材长视频 Premium direct | 源时长 200s，目标 Premium | 轻量规划 | direct、完整 Premium 门禁 | P0 | 通过 |
| TC-005 | 素材长视频 Relay | 源时长 200s，目标非 Premium | 轻量规划 | 冻结同语言 Premium Relay | P0 | 通过 |
| TC-006 | 素材无 Relay 部分容量 | 一个长候选无 relay，后续有短候选 | 规划 | 跳过当前，继续得到非空子集 | P0 | 通过 |
| TC-007 | 短剧未知时长路由 | Premium/非Premium目标 | 规划 | 前者 direct，后者同语言 Relay | P0 | 通过 |
| TC-008 | deferred 权限边界 | 普通 enqueue/manual/daily 伪造 deferred | 建队 | 400 拒绝；schedule-plan 可用 | P0 | 通过 |
| TC-009 | 历史 preflight 严格 | 正式批次缺指纹或内容漂移 | publish | `media_preflight_changed`，零 X 写 | P0 | 通过 |
| TC-010 | deferred 真实探测 | hint 与实际时长不同 | publish | 只下载/probe一次，按实际时长生成 af_channel/category | P0 | 通过 |
| TC-011 | 第一条 known fail 继续 | 两条 material queues | 第一条下载/probe失败 | 第二条仍发布，run completed_with_errors | P0 | 通过 |
| TC-012 | 第一条 drama known fail 继续 | 两条 drama queues | 第一条失败 | 第二条仍发布，失败剧局部错误，不全局 needs_review | P0 | 通过 |
| TC-013 | drama unknown 停止 | 第一条 unknown | 执行 | 后续不调用，全池 needs_review | P0 | 通过 |
| TC-014 | 429 停止 | 第一条 429 | 执行 | 后续不调用，run stopped | P0 | 通过 |
| TC-015 | 历史媒体错误复检 | repair/download类旧码 | 新计划选中 | 原子清旧码并建队；unsafe/mapping仍不可跳 | P1 | 通过 |
| TC-016 | 同批次恢复幂等 | claimed run 无 queue | 重启新 runner | 同一 run 建队；重复调用读回原队列 | P0 | 通过 |
| TC-017 | SQLite 迁移 | 旧 DB 无新列 | 初始化 store | 加列成功，历史行默认 preflight，integrity/FK 正常 | P0 | 通过 |
| TC-018 | X 全量回归 | 所有 X 自动化测试 | 执行 discovery | 全部通过或仅既有条件 skip | P0 | 通过，729/729；skip 2 |
| TC-019 | 坏绑定剧不遮挡 | 一部绑定剧 metadata invalid，另有健康绑定/未绑定剧 | 执行 drama schedule | 坏剧绑定/进度不变，健康账号有序子集建队并发布 | P0 | 通过 |

## 回归范围

账号 OAuth/Token、素材池/短剧池、manual/X Auto、daily/catchup、Relay/Repost、短链、日志/聚合、未知结果与恢复接口。
