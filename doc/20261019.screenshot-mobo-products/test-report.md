# 测试报告

最新规则更新：两个Mobo产品已从>3000下调为>1000，不限ROAS；25项回归通过。重跑2026-10-18后Moboreels达标2、新增1、跳过1，MoboShort达标0，提交失败0。详见mobo-spend-1000.md；下文首次上线结果保留原>3000历史口径。

最终结果：两产品六图真实上传与正式入库验收通过，三产品每日定时已启用；昨天2026-10-18实际提交DramaWave1条、跳过24条，两个Mobo产品各0条，提交失败0。25项功能测试与5项模型回归通过。历史阻塞及修复过程保留在下文。

## 离线验证

2026-10-19：Windows/Linux 新增功能及部署守卫测试 25/25；原截图模型兼容回归 5/5；Python 编译、截图页面内联 JavaScript node --check、git diff --check 均通过。恢复预检覆盖固定failed任务及六张本地文件SHA，接收查询覆盖外部material_id与初始type=2。生产快照完整组合及未修改函数 AST 比较通过。

## 已完成生产验证

- GitHub 精确提交 2ea151cd5bf910729eb4dc4a76dbf3ffb7688b27 已在 CPU 43.166.187.96 部署。
- 新封面产品API返回1479/979/3360/2373；原视频产品API仍1479/979；无凭证访问返回401。
- 公网页面和已登录浏览器均显示四产品；新产品任务与名称正确显示。
- 四个图像 worker PID保持不变，未调整模型/并发。主API窄重启正常。
- 昨日（2026-10-18）dry-run：DramaWave 25候选、24已有、1可新增；Moboreels与MoboShort各0候选。三个profile的阈值和运算符符合需求。

## 真实验收被上游产品限制阻塞

此段为早期验收记录；上游已修复，最终验收和激活结果见末尾“最终验收通过”。

首个原封面触发上游图像内容审核，保留原始失败任务、不重提同一任务。已另选一部普通海报，以独立 canaries-alternate.json 冻结两产品任务；未调整审核规则。各产品一次single/batch创建用于同时验证两入口。

素材入库会复制图片到新的COS路径，故不能用原图URL相等判断入库。核对稳定的content_id+完整素材名（含job前缀、比例）、目标product、type=1，并下载入库URL比对生成图片SHA256。

备用剧两产品的六张封面均已生成且完成公网解码、尺寸和SHA校验，但 ai-source 均返回 HTTP 200/code=-1（素材app_package不支持导入），正式及接收素材匹配数为0。旧逻辑因此误标done，回执修复9242380bffa2876ff9a95dfcbeab02540e04ec23已部署，两条本次验收记录已纠正为failed/progress=96且各有审计；图片和素材标识保留，不重生成。通用 source 接口明确禁止短剧，不能作为替代。

用户已确认上游接口由其开发调整。后续仅恢复本次两任务的脚本、上游字段说明及启用步骤已准备，详见upstream-handoff.md；脚本默认只读，不自动等待或触发上传。

服务器 acceptance-tools 独立干净检出完成只读恢复预检：两条固定failed任务、六张本地图片SHA和公网冻结值一致、相关接收/正式素材0条。未执行--apply。最终验收只计data_source=6的正式图片，不将102接收行计作完成。

待完成：上游支持两个包名、仅上传已生成图片、六图/六素材真实回读、--activate、昨日首轮和后台交接。在此之前不启用多产品自动化，也不宣称真实验收成功。

## 技能与回滚

已备份并更新本地 ai-backend-maintenance 共享上下文，记录独立阈值、无分集封面、只读去重和分阶段上线规则；未提交该技能仓库其他既有未提交修改。回滚命令和私有证据位置见 deploy.md。

## 2026-10-19 接口放开后的续验

17:16（北京）用户确认接口已调整并授权复用图片验收。新备份before-resume-20261019-171633保存SQLite、部署状态和原定时wrapper/crontab。两条原任务仅调用一次恢复API：preserved_count=3、retry_count=0、force_remake=false，回调均HTTP200/code=0；六图SHA与原冻结值完全一致，没有重新生成。

接收记录为7680927–7680932，正确归属moboreels/MoboShort，但data_source=102的data_source_id为外部material_id、type=2，需由上游异步转正式素材。17:21回读正式素材仍0条，接收remark为空；比其晚接收的DramaWave已转正式。已向用户提供接收ID排查异步worker，期间不重发、不activate。

只读验收/恢复工具已补接收标识查询，Windows/Linux测试25/25通过（2ea5a8e）。最终仍只计data_source=6/type=1正式图片。

昨天2026-10-18的三产品只读预检：DramaWave25候选、24已存在、1可新增；Moboreels0、MoboShort0。阈值分别为>=1000且ROAS>=45%、>3000且不限ROAS、>3000且不限ROAS。服务器Asia/Shanghai，crond active，原10:00/22:00计划未改。

## 最终验收通过

用户再次确认后，17:25回读六条正式素材均已存在。两条任务done，六张公网图可解码、尺寸分别1200x1200/1200x628/1200x1500，正式入库图片SHA256与原始生成图完全一致。复用原任务和material_id，没有重复上传或重新生成。

| 产品 | 1:1 正式素材 | 1.91:1 正式素材 | 4:5 正式素材 | 剧ID |
| --- | --- | --- | --- | --- |
| moboreels / 3360 | 7681052 | 7681057 | 7681055 | 67767322 |
| MoboShort / 2373 | 7681054 | 7681053 | 7681056 | 67767322 |

正式素材全部data_source=6/type=1，已生成accepted=true的acceptance.json。

17:26:28（北京）执行原部署提交9242380的deploy.py --activate，三产品wrapper已经生效；crond active，Asia/Shanghai，既有`0 10,22 * * *`时刻不变。本轮没有重启API或生成worker。激活前备份before-activation-20261019-172628，wrapper SHA256=5a349d6088432bb06f0324483201f6c932a40dd641ab21b59a9651a1a502b6a4。

## 昨日首次正式运行

实际执行wrapper --all-products --date 2026-10-18，exit=0；输出first-run-20261018.json，出站前intent、stderr、exit-code及任务库readback独立保存。仅前一个完整北京自然日，未补更早历史。

| 产品 | 消耗条件 | ROAS条件 | 候选 | 跳过已有 | 新增任务 | 提交失败 |
| --- | --- | --- | --- | --- | --- | --- |
| DramaWave | >=1000 | >=45% | 25 | 24 | 1 | 0 |
| Moboreels | >3000 | 无 | 0 | 0 | 0 | 0 |
| MoboShort | >3000 | 无 | 0 | 0 | 0 | 0 |

DramaWave新增剧K2DWdXyOhQ，消耗4414.74、AF D0 ROAS45.21%；新增任务持久化为a2a5968db3ce4f158719d56be3840364，按app_id+content_id唯一核对。后续生成和上传交现有后台worker，不在聊天里持续轮询整批。两个新产品无候选已明确记录零任务。

17:28生产回读：四产品API正确、API与crond active、四个图像worker PID与初始部署完全一致。文档、上游交接及技能上下文已同步到最终启用状态。
