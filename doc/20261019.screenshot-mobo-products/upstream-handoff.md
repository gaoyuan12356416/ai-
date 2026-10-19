# 素材接口改动与后续启用

2026-10-19 用户确认素材接口由其开发调整；封面功能其余开发完成，先保持现有 DramaWave 定时入口，待真实上传通过后启用两个新产品。

## 上游需支持

沿用 `POST https://aa.yingliangads.com/api/material/ai-source`，现有鉴权与 JSON 数组格式不变。

| app_id | app_package | 素材归属产品 |
| --- | --- | --- |
| 3360 | com.changdu.mobovideo | moboreels |
| 2373 | com.changdu.moboshort | MoboShort |

每条包含 app_package、稳定 material_id、content_id、category（1:1/1.91:1/4:5）、name、url、email、created_at。实际 payload 见 app.py 的 post_screenshot_ai_source_callback。无需给 API 调用方新增产品字段，不改变 DramaWave/FreeReels 包名。接收和正式素材均须关联正确剧 ID、目标产品和图片 type=1；material_id 重复需保持原有幂等语义。

当前两个包名均返回 `HTTP 200 / {"code":-1,"msg":"素材app_package不支持导入!","data":[]}`。封面端已修复业务失败假完成。上游成功需明确返回 code=0，验收仍会独立回读正式入库及图片，不把 HTTP200 或仅接收记录当作验收成功。

## 已保留的验收资产

两产品各一条任务、同一剧 67767322，每条三图已完成公网解码、1200x1200/1200x628/1200x1500 尺寸及 SHA256 校验。实际任务 ID、回执、图片清单和空素材回读存在 CPU 私有任务目录：`/mnt/data-disk/codex/tasks/screenshot-mobo-products-20261019`。

因包名明确被拒绝，两任务已纠正为 failed，保留全部图片和原素材 ID。不要删除任务、换 ID 或重新生成，不重试前一部因图像审核失败的剧。

## 接口放开后的步骤

1. 从 GitHub 获取含 resume_acceptance.py 的提交，先执行该脚本默认只读预检。它检查两条固定失败任务、本地六图与冻结SHA一致、素材库没有相关记录；有异常就停止。
2. 明确确认上游已支持两个包名后执行 `resume_acceptance.py --apply --upstream-ready`。每条只调用一次既有 retry API，要求回执 preserved_count=3、retry_count=0、force_remake=false。不重放未知结果；有 canaries-resume.json 时先对账。
3. 任务结束后执行 `acceptance.py check --manifest-name canaries-alternate.json`，须两条完成、六张图片正确、六条正式素材与产品/content_id/名称/SHA一致。只有成功才生成 acceptance.json。
4. 使用 deployment.json 对应部署提交的 `deploy.py --activate`，它校验真实验收，再替换三产品 wrapper；不修改既有10:00、22:00 crontab。
5. 立即调用 `/root/drama_material_service/scripts/run_auto_cover_synthesis.sh`，默认取启用当天的前一个完整北京自然日；将完整输出保存为带日期的 first-run JSON。不要固定为开发时的2026-10-18，也不补更早历史。空候选记录零任务；其余批次交已有后台worker处理。

当前精确部署源码仍为服务器 `source` 的 9242380；仅文档/运维辅助脚本的新提交可使用单独干净 worktree，不能随意改变原部署清单的commit或绕过激活版本守卫。
