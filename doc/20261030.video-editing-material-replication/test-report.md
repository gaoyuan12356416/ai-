# 测试报告

日期：2026-10-30。状态：本地回归、评审、生产部署及历史复用验收完成，入口已开放。

## 已执行本地证据

- 部署代理执行：`python -m unittest discover -s tests -p test_video_editing_material_replication_deploy.py -v`，15 项通过，覆盖只读预检、SHA/白名单、WAL 在线备份、迁移保留旧数据、局部失败恢复、回滚保留新记录、旧 worker PID、marker 开放及丢回执核对。
- 本机没有 pytest；原 pytest 命令报 No module named pytest，随后采用项目已有 unittest，不需要新增依赖。
- 后端最终合并回归：`python -m unittest discover -s tests -p 'test_video_editing*.py' -q`，270 项，1 项跳过，其余通过。
- 主代理执行历史导入专项 6 项、部署专项 15 项，均通过；命令为 `python -m unittest discover -s tests -p test_video_editing_replication_import.py` 及上文部署测试。
- 前端执行 `node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`，75 项通过，包含历史成功记录后的新请求失败及未知结果状态。
- `git diff --check` 已执行，无空白错误；仅 Windows LF/CRLF 提示。发布脚本读取 git show 原始字节。

## 本地浏览器交互证据

主代理使用正式 static 页面在 `http://127.0.0.1:8769` 的本地测试接口环境验证，未访问外部生产接口。1440×1000、820×1180、390×844 均无横向溢出，提交区在可视范围内；重开滚动归零且目标语种为空，源语种 en 可选择，竞品 + AI + 解说毫秒组合摘要正确，本地提交与历史交互可用，控制台错误 0。

工作区截图：
- outputs/production-replica-desktop.jpg
- outputs/production-replica-tablet.jpg
- outputs/production-replica-mobile.jpg

上述是本地正式页面与测试接口的证据，不是已登录生产浏览器验收。生产浏览器当前未登录。

## 生产部署与验收

- 运行提交：`4cff9d3a10af861d4d42184a5deee2f57d4a1461`；GitHub 已推送，CPU 从同 SHA 的独立 checkout 安装。后续文档提交不改变运行代码。
- 开放时间：2026-10-30 14:55:00 +08:00。入口 `https://ai.yingliangads.com/video-editing.html` → 制作任务 → 素材复刻。
- 备份：`/mnt/data-disk/video-editing/backups/material-replication-20261030T065100Z-4cff9d3a10af`。含代码 manifest、SQLite 一致副本及迁移演练副本；演练旧表逐表校验不变。数据盘 UUID 符合要求，预检可用约 520.88 GiB。
- 14 个目标文件/配置均与 manifest 一致；公共 HTML/JS/CSS 三文件 HTTP 200 且 SHA256 与运行文件相同。未登录历史 GET 返回 401。
- 主 API PID 3810696、新复刻 worker PID 3810821 均 active；原渲染 worker PID 1946356、原入库 worker PID 2053482 保持不变。
- 仅新 worker 进程包含 B 组凭据；主 API 进程不包含 A_GROUP_TASK_CREATE_TOKEN。配置存在不等于重新验证外部凭据，本次不发送新生产请求。
- dry-run、首次导入和重复导入均通过。账本始终仅两条：gaoyuan/rejected；susiqi/accepted（B 任务 1732014、素材任务 2080677）。两次导入返回相同 ID；没有 pending/submitting，没有新提交记录。
- 实际管理员身份从本地用户表核实。保持磁盘入口关闭，在独立进程内覆盖 enabled，禁止新账本行插入并封锁全部 requests HTTP，再调用真实 service 和只读剧目目录：resource_id=28331、source=en、duration_ms=1789867、归属 susiqi，目录返回 20 个去重语言候选。
- 原请求的新 operation 复用记录 `vmr_35480764a29a4109bef5a946e596c5b3` / 任务 1732014；同 operation 再次返回同记录。相同 B 键改 operator=gaoyuan 返回原 susiqi 任务及归属差异。HTTP 调用 0，仅新增两条操作别名映射。
- 开放后复核：账本 accepted=1/rejected=1，historical=2，非历史提交=0。母任务、成片、原入库三行与部署前备份完全一致；所有服务 active，运行文件散列一致。

备份目录中的独立证据：`history-import-first.json`、`history-import-repeat.json`、`acceptance-history-reuse.json`、`public-readback-before-enable.json`、`final-readback.json`。

## 精确回滚入口

```bash
python3 -B /mnt/data-disk/video-editing/releases/4cff9d3a10af861d4d42184a5deee2f57d4a1461/scripts/deploy_video_editing_material_replication.py --rollback /mnt/data-disk/video-editing/backups/material-replication-20261030T065100Z-4cff9d3a10af/manifest.json
```

该命令关闭入口、停止新增 worker 并恢复本次代码，保留当前 SQLite 新账本及所有回执；不要用 SQLite 备份覆盖运行库。

## 限制与发布建议

本次功能只提供提交回执和历史，不监测成品或回推。真实新任务请求未作为自动回归的一部分。生产浏览器未登录，业务验收采用上述真实身份/真实目录/真实账本的进程内服务链路；响应式交互采用正式静态代码和隔离测试接口，不能等同于已登录生产浏览器全流程验收。
