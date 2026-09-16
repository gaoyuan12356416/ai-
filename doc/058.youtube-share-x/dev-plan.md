# 实施计划

主任务负责宏、主桥接、Cookie路由/精简DTO、测试、部署和文档；界面工作流负责原三份页面资源和Node交互用例；X服务工作流负责持久台账/内部客户端/发送与恢复；独立评审检查实际适配器和异常路径。

构建：Python compile/py_compile；node --check。验证：三套新功能用例、既有YouTube HTTP/工作流/素材筛选与X账号契约、实际浏览器只读验收。

发布：GitHub推送→CPU精确拉取commit→校验基线→备份代码/SQLite→暂停相关X触发器并确认无发送中任务→切换API/X sidecar→恢复触发器→健康/文件/页面/台账验证。
