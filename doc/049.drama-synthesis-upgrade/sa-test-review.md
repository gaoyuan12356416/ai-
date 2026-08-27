# SA 测试用例评审

## 最新覆盖范围：现有账号 v3（2026-08-27 16:35）

按用户新决定与 [现行合同](ads-ai-new-tables-20260827.md)，不再创建专用数据库账号。CPU 使用现有 ads_aius 与已有频道授权，应用 SQL 仅限 ads_ai 新三表；原 MySQL 表只读。健康合同为 drama-youtube-writer-preflight-v3，shared-existing-account / application-table-allowlist / db_least_privilege=false；仅核验必要能力，不宣称全量 grant 审计。每次写前验证 TRIGGER 可见性和无 trigger/FK，旧健康合同拒绝。既有 DDL/v2 payload 与 UI 合同不变；下文专用账号/旧 v2 health 是历史。本轮专项 108/108，独立唯一完整回归及实机发布验收另记，不叠加历史批次。

## 最新 ads_ai 增量（冻结代码验收通过）

新增新三表为空时创建、兼容重复执行、冲突对象停止、完整 Unicode/长 URL/描述回写、乱序与幂等、异内容拒绝、额外权限/v1/旧库失败关闭、旧迁移入口拒绝且零网络。实机新建表演练与生产最小身份健康分别验收，演练不假称真实生产 writer 身份。独立 QA 在实现冻结后唯一执行完整回归；旧结果不叠加。见 [新合同](ads-ai-new-tables-20260827.md)。

执行结论：262/262一次合并PASS，另15/15纯内存安全对抗、35文件语法/3.9AST和18文件冻结SHA一致均通过。针对direct store/遗留canary坏actor的检查在claim/OAuth前拒绝。真实CPU Python3.9/MySQL5.7及外部平台验收由部署阶段补齐。

## 2026-08-27 CPU 查询边界

新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 的目录用例覆盖 metadata-only、315/无 light、GPU 目录/配方 identity 等价、CPU 无 HTTP/DB/素材访问、缺配置/错 SHA 无 GPU fallback、GPU 本地诊断、文件类型/长度/竞态/坏 JSON。独立最终七套 204/204 PASS，15 项额外内存对抗单列 PASS；原 16 专项与 188/166 不叠加。

主代理随后 CPU Python 3.9.6 真实文件+精确 app 原函数验证 PASS（不是生产 HTTP 接口验收）。未改页面，未重跑历史浏览器或 HK 媒体。生产部署、数据库合法账号和指定频道真实发布仍为后续门禁，不能由本轮代码/目录测试代替。证据详见 [测试报告](test-report.md)。

## 历史 Wave8

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 独立 QA PASS，0 P0/P1。独立证据为 focused 45/45、broad 77/77、Playwright 3/3、compile 11/11、Python 3.9 AST 11/11、spec syntax 1/1、inline JS 4/4、writer 3 个正常 + 26 个 adversarial、outbox 9/9。

必须拒绝旧断言：旧短链域/target、singular route、top-level random config、旧 result 字段、默认三项选中、video ID 即成功、旧短状态/旧表、无 processing/outbox/macro 的成功路径。

Wave8 候选与线上实查增量 code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 的代码/测试合同均已独立 PASS；增量第四轮 P0/P1/P2=0/0/0，精确证据以 `test-report.md` 为准。固定 public 合规风险和未完成的统一表生产门禁继续 HOLD；测试没有执行任何真实短链、MySQL 写入或 YouTube 外部发布。
