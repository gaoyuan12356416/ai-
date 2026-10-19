# 部署与回滚

CPU 43.166.187.96。执行 ops/screenshot-mobo-products-20261019/deploy.py，默认仅预检；--apply 部署。必须由服务器 GitHub 拉取的干净精确提交执行。

备份 /mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019/before-deploy：相关源码、两份静态页面、crontab 和在线 SQLite 备份。部署清单记录每个文件前后 SHA256、提交和图片 worker PID。数据盘 UUID、空间、待处理任务、旧函数 AST、runner hash 都是发布门槛。

仅重启 drama-material-api。四个图片 worker、模型、并发和数据库不变。失败自动恢复文件，后续人工回滚执行同一提交 deploy.py --rollback，保留当前数据库和所有图片。先确认封面及视频任务已结束。

真实验收通过后启用三产品定时入口并运行一次昨天。不得对已接受但响应不确定的请求盲目重发；先回读任务库。

部署分两阶段：--apply 保持旧 wrapper，真实验收通过并写出 acceptance.json 后执行 --activate。回滚会校验文件指纹，遇到发布后的其他改动则拒绝覆盖，需重新审阅局部回滚。

## 已部署代码与证据

- 功能提交 cbb3de7f62fcc7877ace7d98e2b1397337379044；部署修复后实际运行提交 2ea151cd5bf910729eb4dc4a76dbf3ffb7688b27。
- CPU 拉取源码：/mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019/source。
- 已完成 --apply，/api/drama-screenshot-material/products 返回四个产品；原视频 products 仍两个；未登录返回401；公网截图页使用新接口。
- 四个截图 worker PID 未变。多产品定时入口待真实两产品验收通过后执行 --activate。
- 精确回滚：python3 /mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019/source/ops/screenshot-mobo-products-20261019/deploy.py --rollback。
- 生产验收、API 回读、试跑和部署清单均存放于上述任务目录，包含私有任务证据，不提交至 GitHub。

## 回执修复与上游门槛

真实验收发现 ai-source 不支持两个新增包名，HTTP200/code=-1。repair_callback.py 从 GitHub 精确提交局部替换回调函数，备份 before-receipt-fix，窄重启 API，保持四个 worker PID；仅纠正本次两条已明确拒绝的验收任务，不发送请求，保留六图。同步更新 deployment.json 的代码指纹和版本，使原始完整回滚仍可使用。

未经六图与六条正确产品素材的验收，不得执行 --activate。不能切换到明确排除短剧的通用 /api/material/source，也不能改用 DramaWave 包名。待上游增加两个产品后，只回传已有图片并保留原 material_id，先对账再恢复本次验收。

回执修复已部署并回读确认：9242380bffa2876ff9a95dfcbeab02540e04ec23，app.py SHA256=84e27623cc06313abc36b2395b59889f1c8638ac2f3cea6dbdc7451e3cd13d72。两个canary现为failed/progress96，六图不变，各有纠正审计；多产品入口仍未激活。上游由用户安排开发，交接与后续命令见upstream-handoff.md。

## 2026-10-19 最终激活

上段为回执修复时状态。17:26:28北京时间，两个canary已经done、六条正式素材验收通过，执行原部署提交9242380的--activate成功；deployment.json记录cron_products_activated=true、upload_acceptance=verified。

CPU同一服务目录的wrapper现与GitHub源码一致（SHA256=5a349d6088432bb06f0324483201f6c932a40dd641ab21b59a9651a1a502b6a4），无参数时运行三个固定profile。crontab保持`0 10,22 * * *`，服务器Asia/Shanghai，crond active；本轮无服务重启。

恢复验收前新增在线备份before-resume-20261019-171633；激活前备份before-activation-20261019-172628保存原wrapper、crontab及部署状态。均位于/mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019。

若仅撤销新增产品的定时运行：持/root/drama_material_service/logs/auto_cover_synthesis.lock排他锁，核对现wrapper哈希为上述值，再将before-activation-20261019-172628/run_auto_cover_synthesis.sh复制回/root/drama_material_service/scripts/run_auto_cover_synthesis.sh；无需改cron或重启API。保留全部任务/图片/素材。完整功能回滚仍使用原source/ops/screenshot-mobo-products-20261019/deploy.py --rollback，守卫拒绝覆盖后续他人代码改动。
