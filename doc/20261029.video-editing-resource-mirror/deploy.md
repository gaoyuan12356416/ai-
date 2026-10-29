# 发布与兼容回退

主分支codex/video-editing-resource-mirror-20261029；GPU复合分支codex/video-editing-resource-mirror-gpu-20261029，从实际05f48e2175c691534a88549a694cfe9d4cc6739d派生，仅覆盖recipe.py、render.py、gpu_integration.py及发布脚本/说明。

CPU /root/drama_material_service是复合运行目录，只安装限定功能文件和两份video-editing静态资源。SQLite仍位于/mnt/data-disk/video-editing/state.sqlite3；业务MySQL不DDL。先GitHub推送确切提交，再服务器fetch/checkout。

使用scripts/deploy_video_editing_resource_mirror.py执行两端只读check；CPU prepare关闭新剪辑入口并排空相关队列，GPU prepare/apply安装兼容渲染器，CPU apply安装接口和页面，最后GPU restore、CPU restore恢复原入口状态。入库未知账本保留读回核实，不重发。只有相关worker/API按需重启，不要求无关CPU任务清空。

出现新任务后回退只撤回新入口/静态页面，保留v2和旧profile兼容读取器及数据库/COS。不得恢复旧SQLite快照或覆写历史任务。具体commit、备份、执行回执和最终健康检查在实际部署后补录。
