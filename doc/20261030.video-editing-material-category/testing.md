# 测试与证据

2026-10-30 本地专项：
- 部署代理执行 python -m unittest discover -s tests -p test_video_editing_material_category_deploy.py -v：Ran 12 tests，OK。
- 关键覆盖：7 个白名单目标、复合部署文件漂移拒绝、只读预检、分支/SHA、原开关保留、BEGIN IMMEDIATE 排空屏障、排空超时不停止 worker、局部失败恢复、分类回滚保留新回执、坏备份先拒绝、无迁移、旧 worker 不停止。
- 主代理报告此前 Python 合并回归 Ran 279，skipped=1（不是“279 项全部通过”）；Node 82 项通过。该统计先于本次 12 项部署测试，分别保留原统计，不将其相加称为同一轮合并测试。本次补充生产证据未重新运行测试。
- git diff --check 无空白错误，只有 Windows LF/CRLF 提示。

主代理使用本地正式 static + 测试接口完成 CUA 验证：7 个精确选项，默认混剪；分类选解说时 target_material_type 仍为 1；image 回执显示图片（image）；真正关闭重开后恢复混剪、目标 0、滚动 0；旧历史显示未指定；旧未指定→精剪冲突明确说明新参数未生效，未新增测试记录。
1440×1000、820×1180、390×844 均无横向溢出，固定提交区可见；手机普通点击滚动可操作分类。控制台错误 0。
截图在工作区 outputs/material-category-desktop.jpg、material-category-tablet.jpg、material-category-mobile.jpg。

以上浏览器交互均为本地隔离 fixture 证据。实际访问线上浏览器时未登录，不能据此声称完成已登录生产浏览器验收。

## 生产部署与真实账号验收

2026-10-30 主代理完成部署并保存生产证据：

- 发布 SHA：`d4bc266f26607efe339ab248ffe40e811835c69d`；CPU checkout：`/mnt/data-disk/video-editing/releases/d4bc266f26607efe339ab248ffe40e811835c69d`。
- manifest：`/mnt/data-disk/video-editing/backups/material-category-20261030T083542Z-d4bc266f2660/manifest.json`，状态 `installed`；7 个目标文件哈希校验通过，复刻 gate 恢复开放。
- 发布后 API PID `3945713`、复刻 worker PID `3945762`；原渲染 worker PID `1946356`、原入库 worker PID `2053482` 均未变化。
- 使用本地用户表核实的 active admin 真实 actor 执行生产进程内业务链路，`service.material_replication.context` 返回 enabled，默认归属为 `susiqi`。
- 无分类的旧 operation 复用既有任务 `1732014`，DTO 分类为 null；同 operation 追加“混剪”返回 409，原参数不变。
- 新 operation 显式分类 `image` 仍复用旧记录，唯一冲突字段为 `material_category`，previous=null、requested=image；再次请求仍复用，新分类未写入旧记录。
- 验收进程硬禁止 `requests.Session.request` 及新增 `material_replication` 的 `store.insert`：HTTP 出站 0、新订单 0。
- 账本仍为 accepted 1 / rejected 1，两条全文行未改；原 task/output/material_upload 与备份一致。operation alias 从 4 增至 5，仅增加一条复用别名。
- 公网三份静态资源 GET 均为 200，内容与已部署 SHA 匹配；未登录业务 API 返回 401。此项证明公开资源和登录边界，不替代授权业务验收。

业务证据与公开资源证据分别保存在上述备份目录的 `category-acceptance.json`、`category-public-verification.json`。本次验收未创建新的 B 组任务；分类回滚的精确命令及保留 SQLite 回执规则见 `deploy-review.md`。
