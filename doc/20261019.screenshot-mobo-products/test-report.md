# 测试报告

## 离线验证

2026-10-19：Windows/Linux 新增功能及部署守卫测试 24/24；原截图模型兼容回归 5/5；Python 编译、截图页面内联 JavaScript node --check、git diff --check 均通过。第24项验证验收恢复仅限固定failed任务并要求六张本地文件与冻结SHA完全一致。生产快照完整组合及未修改函数 AST 比较通过。

## 已完成生产验证

- GitHub 精确提交 2ea151cd5bf910729eb4dc4a76dbf3ffb7688b27 已在 CPU 43.166.187.96 部署。
- 新封面产品API返回1479/979/3360/2373；原视频产品API仍1479/979；无凭证访问返回401。
- 公网页面和已登录浏览器均显示四产品；新产品任务与名称正确显示。
- 四个图像 worker PID保持不变，未调整模型/并发。主API窄重启正常。
- 昨日（2026-10-18）dry-run：DramaWave 25候选、24已有、1可新增；Moboreels与MoboShort各0候选。三个profile的阈值和运算符符合需求。

## 真实验收被上游产品限制阻塞

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
