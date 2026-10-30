# 测试报告

日期：2026-10-30。状态：本地回归与评审完成，生产发布证据待发布后补充。

## 已执行本地证据

- 部署代理执行：`python -m unittest discover -s tests -p test_video_editing_material_replication_deploy.py -v`，14 项通过，覆盖只读预检、SHA/白名单、WAL 在线备份、迁移保留旧数据、局部失败恢复、回滚保留新记录、旧 worker PID、marker 开放及丢回执核对。
- 本机没有 pytest；原 pytest 命令报 No module named pytest，随后采用项目已有 unittest，不需要新增依赖。
- 后端最终合并回归：`python -m unittest discover -s tests -p 'test_video_editing*.py' -q`，270 项，1 项跳过，其余通过。
- 主代理执行历史导入专项 6 项、部署专项 14 项，均通过；命令为 `python -m unittest discover -s tests -p test_video_editing_replication_import.py` 及上文部署测试。
- 前端执行 `node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`，75 项通过，包含历史成功记录后的新请求失败及未知结果状态。
- `git diff --check` 已执行，无空白错误；仅 Windows LF/CRLF 提示。发布脚本读取 git show 原始字节。

## 本地浏览器交互证据

主代理使用正式 static 页面在 `http://127.0.0.1:8769` 的本地测试接口环境验证，未访问外部生产接口。1440×1000、820×1180、390×844 均无横向溢出，提交区在可视范围内；重开滚动归零且目标语种为空，源语种 en 可选择，竞品 + AI + 解说毫秒组合摘要正确，本地提交与历史交互可用，控制台错误 0。

工作区截图：
- outputs/production-replica-desktop.jpg
- outputs/production-replica-tablet.jpg
- outputs/production-replica-mobile.jpg

上述是本地正式页面与测试接口的证据，不是已登录生产浏览器验收。生产浏览器当前未登录。

## 生产验证待补

- 已推送 GitHub SHA、CPU 独立 checkout SHA、白名单文件读回。
- 数据盘挂载、SQLite backup、迁移副本证据、真实备份 manifest。
- 主 API/新 worker 与原 worker PID/状态。
- 历史导入 dry-run/apply/重复导入、实际 actor 进程内 API 链路、同键复用和冲突证据；应无新增生产请求。
- 公共未登录接口 401 与公开静态文件校验。不得把公共 401 标成授权业务验证。
- 开放 marker 时间与精确回滚命令。

## 限制与发布建议

本次功能只提供提交回执和历史，不监测成品或回推。真实新任务请求不作为自动回归的一部分。待最终回归、关闭部署、历史导入及复用验证均完成后，按已授权计划开放入口；未完成前不声称生产发布成功。
