# QA 验证报告

日期：2026-10-29。状态：本地完整模块与独立专项验证通过，Linux/生产结果待主代理补充。

## 独立执行结果

在 server-monitors/meta-violation-material-cleanup 下运行 unittest discover 对应文件：

| 文件 | 结果 |
|---|---|
| test_cover_worker.py | 35 项通过 |
| test_cover_pipeline.py | 8 项通过 |
| test_cover_daily.py | 19 项通过 |
| test_cover_scheduler.py | 7 项通过 |

共69项专项通过；git diff --check通过。事件型用例证明清理阻塞时广告继续、慢广告不形成整波屏障、SQL可与网络重叠、停止/让锁排空、共享Governor唯一、旧绑定读回不重复POST、过期来源不写和并发指标不双计。未用这些专项结果冒充生产吞吐结论。

## 发现与修复

- BUG-001：accepted后旧绑定读回导致全局停机，修复为unknown隔离及只读核实，保留一次POST和共享fence。
- BUG-002：滑动补位初版可能每完成一条就单条来源查询；修复查询触发条件，错峰补位用例通过。
- 清理新增同Creative未完成任务过滤，避免过早引用扫描及小时暂缓；废扫描未提交行不永久阻塞。主代理报告对应store33项通过。

## 只读性能证据

同一64任务/64账户对照：两批32共12.337秒，单批64为4.597秒；64条有效且准备结果完全一致。这仅是一次来源准备对照，不保证整体持续吞吐。

## 尚需记录

Linux候选验证、GitHub提交/拉取hash、备份/回滚路径、暂停样本核实及恢复、两个lane实际持续执行样本。历史waiting_references检查时间、已有失败/未知不复位。
## 主代理完整本地回归

主代理实际运行 `python -m unittest discover -s tests`：728 项，36.047 秒，OK（1 项跳过）；compileall 和 git diff --check 通过。随后补入的低水位查询修复另经独立 scheduler 7 项测试通过。Linux 候选和生产部署尚未执行，当前结论仅为本地验证完成。
## 窄部署路径评审与候选Linux验证

通用prepare因另一后台删除任务活跃及API无关查询/审批方法已演进而拒绝；这不要求暂停其他任务。本次新增worker专用prepare，仅封面模块切换，原通用部署gate不修改。静态审查确认：candidate/current封面Store精确同hash，API相关封面保护AST比较在prepare前后及apply复验；不实例化共享Store、不修改API或业务账本，只做一致快照备份。rollback仅恢复代码链接/units，保留备份后新增回执。

独立运行 test_cover_worker_install.py：5项通过，包括无关active任务可保留、候选Store变化拒绝、封面service活跃拒绝、API保护变化拒绝、回滚不覆盖新回执。主代理报告候选业务release55d706f在Linux运行完整728项全部通过，无跳过。新窄部署代码尚待对应候选服务器验证与真实切换，生产仍未验收。