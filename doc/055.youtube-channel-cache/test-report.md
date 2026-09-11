# 测试报告

本地：频道/缓存专项19通过；原自动发布engine/service/HTTP共103通过；失败处理47通过；参考图112用例111通过、1 Windows符号链接skip。compile/feature guard通过（5功能22规则）。浏览器频道/缓存/管理员恢复42通过，既有真实6秒轮询闪屏29通过。所有平台调用均mock，不产生真实上传/评论/测试飞书。

独立后端审查三项P2均修复。生产Python3.9、精确GitHub部署和只读接口验收待完成后附录。

状态更新：上述生产验收与自动worker平滑切换均已完成，详见下方生产验收及deploy.md 11:25记录。

复审通过：新增三项P2回归与后台HTTP200错误、强制刷新在途合并修复已验证。

## 生产验收

Python3.9：专项19通过，原自动发布103通过，失败处理47用例46通过+1fixture路径skip；本地真实样本规则和此前同样处理保持。feature guard5功能22规则通过。浏览器mock42/42、真实6秒轮询闪屏29/29。并非登录线上浏览器端到端测试。

真实会话loopback HTTP：topbar3.37ms/bootstrap3.92ms/tasks10.37ms；频道冷返回6.53ms（后台检查中、无条目），约4秒后完整快照，热4.47ms；素材冷返回4.68ms（后台读取中），约6秒后6条素材，热2.89ms。首次冷返回快不等于数据已就绪。初始auth/bootstrap已并行；真实用户网络与整页耗时未测，不将这些数字当作用户端加载时长。

11:20真实频道62条：21 verified、41 blocked（36停用、4手机验证未完成、1频道身份不匹配）。需要处理的在用频道：DramaKuy Mini（220，身份不匹配）；dramawave-New AI Dramas-ysy（249）、Dramawave 2026（251）、CoffeeShorts（258）、Shahrul Ikmal（263）为eligible未达到allowed。CoffeeShorts另有历史403，需Studio功能验证后管理员显式确认恢复。预部署Mini Drama Picks也是eligible，线上复查已变allowed，所以以最新21通过/5在用阻断为准。

公网HTML/JS/CSS均200且与运行文件SHA一致；新频道接口匿名401。只读验收未新增任务或消息。生图任务按滚动策略继续，worker进程切换状态见deploy.md。
