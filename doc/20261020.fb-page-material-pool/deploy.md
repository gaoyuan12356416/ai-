# 部署与回退

目标CPU43.166.187.96，FB /opt/fb-auto-post/current，main /root/drama_material_service，静态 /usr/share/nginx/html。
只部署新模块、FB service组装、调度脚本/单元、指定静态资源、内部路由nginx及main app定点patch。GPU源码不在清单。
GitHub提交后由服务器检出精确SHA。数据盘UUID3e8ac4e8-7770-456d-9e89-2ec5dd405fa8，可写且余量>5GiB。

部署脚本实施前更新；要求：按文件hash保护、备份代码与在线SQLite、在副本演练增量迁移并核对所有旧列旧行摘要；排空旧服务/新发布及prepare租约；保留原timers启停状态。首次pool均暂停，新增scheduler启用不会发无配置计划。

开关FB_VIDEO_V2_ENABLED=1与FB_VIDEO_POOL_ENABLED=1用于新入口，原live/prebuild开关继续生效。回退关闭新增scheduler及两新开关、恢复上版静态、保留兼容backend与已冻结任务；库与短链不降级、不覆盖。若有活跃请求先排空，不能强停Graph结果不明任务。

部署提交、manifest、源SHA、线上验证及准确回退命令待实际完成后填写，不预先宣称上线。
