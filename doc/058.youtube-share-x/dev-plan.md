# 实施计划

主任务负责宏、主桥接、Cookie路由/精简DTO、测试、部署和文档；界面工作流负责原三份页面资源和Node交互用例；X服务工作流负责持久台账/内部客户端/发送与恢复；独立评审检查实际适配器和异常路径。

构建：Python compile/py_compile；node --check。验证：三套新功能用例、既有YouTube HTTP/工作流/素材筛选与X账号契约、实际浏览器只读验收。

发布：GitHub推送→CPU精确拉取commit→校验基线→备份代码/SQLite→暂停相关X触发器并确认无发送中任务→切换API/X sidecar→恢复触发器→健康/文件/页面/台账验证。

## 2026-09-17 标记增量

新增 player_cards.py：受限并发缓存、YouTube 公共页面与只读 API 校验。service/runtime 注入只读展示适配器；精简/完整 DTO 返回 x_player_card，进入 revision。JS 在状态格渲染标记/检查时间，CSS 与 JS 同时更新版本。

验证重点：请求不阻塞、相同视频只排队一次、队列上限、缓存过期、网络错误/身份不符/未公开/地区限制、权限过滤、精简 revision 更新、前端逃逸及过期绿色降级。运行相关 Python / Node 回归，生产重复 Python 验证并读取真实状态。

此增量采用 GitHub-first 主 API 单服务部署。保存 9 个运行代码/静态目标的原始文件及哈希，不切换 X release、不停止任何发布 worker，不恢复或修改业务数据库。
