# SA 测试用例评审

## 2026-08-27 CPU 查询边界

新候选 `40042f9692fbec58caa5abbf41af35e9aefb54bc` 的目录用例覆盖 metadata-only、315/无 light、GPU 目录/配方 identity 等价、CPU 无 HTTP/DB/素材访问、缺配置/错 SHA 无 GPU fallback、GPU 本地诊断、文件类型/长度/竞态/坏 JSON。独立最终七套 204/204 PASS，15 项额外内存对抗单列 PASS；原 16 专项与 188/166 不叠加。

主代理随后 CPU Python 3.9.6 真实文件+精确 app 原函数验证 PASS（不是生产 HTTP 接口验收）。未改页面，未重跑历史浏览器或 HK 媒体。生产部署、数据库合法账号和指定频道真实发布仍为后续门禁，不能由本轮代码/目录测试代替。证据详见 [测试报告](test-report.md)。

## 历史 Wave8

状态：Wave8 exact SHA `85c0b3cda58aeab50765a9ecb09e79a1bbf7e883` 独立 QA PASS，0 P0/P1。独立证据为 focused 45/45、broad 77/77、Playwright 3/3、compile 11/11、Python 3.9 AST 11/11、spec syntax 1/1、inline JS 4/4、writer 3 个正常 + 26 个 adversarial、outbox 9/9。

必须拒绝旧断言：旧短链域/target、singular route、top-level random config、旧 result 字段、默认三项选中、video ID 即成功、旧短状态/旧表、无 processing/outbox/macro 的成功路径。

Wave8 候选与线上实查增量 code SHA `2b26b540660fd3687fa7c66e68a246d1a706136a` 的代码/测试合同均已独立 PASS；增量第四轮 P0/P1/P2=0/0/0，精确证据以 `test-report.md` 为准。固定 public 合规风险和未完成的统一表生产门禁继续 HOLD；测试没有执行任何真实短链、MySQL 写入或 YouTube 外部发布。
