# 测试与证据

2026-10-30 本地专项：
- 部署代理执行 python -m unittest discover -s tests -p test_video_editing_material_category_deploy.py -v：Ran 12 tests，OK。
- 关键覆盖：7 个白名单目标、复合部署文件漂移拒绝、只读预检、分支/SHA、原开关保留、BEGIN IMMEDIATE 排空屏障、排空超时不停止 worker、局部失败恢复、分类回滚保留新回执、坏备份先拒绝、无迁移、旧 worker 不停止。
- 主代理报告此前 Python 合并回归 Ran 279，skipped=1（不是“279 项全部通过”）；Node 82 项通过。该统计先于本次 12 项部署测试，最终合并统计由主代理追加。
- git diff --check 无空白错误，只有 Windows LF/CRLF 提示。

主代理使用本地正式 static + 测试接口完成 CUA 验证：7 个精确选项，默认混剪；分类选解说时 target_material_type 仍为 1；image 回执显示图片（image）；真正关闭重开后恢复混剪、目标 0、滚动 0；旧历史显示未指定；旧未指定→精剪冲突明确说明新参数未生效，未新增测试记录。
1440×1000、820×1180、390×844 均无横向溢出，固定提交区可见；手机普通点击滚动可操作分类。控制台错误 0。
截图在工作区 outputs/material-category-desktop.jpg、material-category-tablet.jpg、material-category-mobile.jpg。

以上均为本地 fixture 证据，不代表已登录生产浏览器验收。上线 SHA、备份、服务 PID、真实 actor 的只读/复用验证和公开资源读回待主代理部署后追加；验收不创建新 B 组任务。
