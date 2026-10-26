# 实现与验证计划

基线9b6ba51合并逐文件核实的线上池黑名单豁免、Page picker与主导航，提交4a2f692。主app.py不修改；仅FB运行目录更新旧auto service.py。

- 来源：GroupPoolSource、<=100 ID/SQL批、既有FIFO broker、语言及组权限。
- 引擎：6张fb_group_pool*增量表；单事务reservation/run/task/link；独立group_pool_v1冻结政策，复用FB worker。
- 前端：共享导航/顶栏、版本冲突、UUID操作恢复、失败保留输入、390/1440布局。
- 接线：精确API白名单、identity导航键、新sidecar端点及10秒systemd调度。
- 部署：GitHub精确提交、SHA前检、SQLite在线备份、排空/时点保护、旧事实比对、窄重启。

验证Python隔离unittest/py_compile、Node语法及浏览器路由mock；线上验证静态/只读API和账本。禁止验收真实外部发布。
