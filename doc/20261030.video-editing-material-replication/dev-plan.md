# 开发计划

1. 后端：新增复刻账本、上下文解析、验证/幂等/受理接口、独立 worker 与 unit；Task DTO 只带本地轻摘要。
2. 前端：将已确认 v2 弹窗接入正式任务卡；目标语种默认空，移除模拟身份/场景/数据，保留原四栏目和入库流程。
3. 历史导入：独立脚本只导入已验证记录与明确归属，不发外部请求；提供 dry-run 和幂等证据。
4. 部署：白名单文件、GitHub SHA 校验、磁盘检查、SQLite backup 与迁移演练、关闭安装、旧 worker PID 核验、保留账本回滚。
5. 独立代码和用例评审后运行后端、前端、部署测试；没有执行证据不能写通过。

## 验证

Python + 原生 JavaScript 项目使用 pytest、Node 浏览器契约测试与语法检查替代 Maven。部署测试入口：
`python -m unittest discover -s tests -p test_video_editing_material_replication_deploy.py -v`。

最终命令、结果与限制写入 test-report.md。发布完成证据须含 GitHub SHA、服务器 checkout SHA、备份目录、服务状态、页面读回和历史复用结果。

## 分工

后端、前端、文档/部署分工；部署脚本负责人不改 features/static/worker unit，正式部署及历史导入由主代理完成。不得自行远程发布。
