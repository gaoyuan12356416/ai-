# Excel 上传解析依赖修复

2026-10-22 的 `excel_parser_unavailable` / HTTP 503 发生在读取表格内容前。
同一 `/usr/bin/python3`（3.9.6）由 root 运行可导入
`/root/.local/lib/python3.9/site-packages/openpyxl`，由实际服务用户
`fb-auto-post`（HOME=/nonexistent）运行则找不到该模块。原部署只检查了
服务器 root 环境，未覆盖实际服务身份。

## 修复范围

- `requirements-fb-excel.txt` 固定 openpyxl 3.1.5、et-xmlfile 2.0.0 及官方 wheel SHA256。
- 将两个依赖安装到既有系统搜索路径 `/usr/local/lib/python3.9/site-packages`。
  先记录目标四个目录不存在、当前服务 PID/timer 状态和部署证据，再安装；
  如果目标已存在，停止并核对，不使用 `--upgrade` 覆盖未知依赖。
- 解析器在每次上传请求中动态 import，无业务代码替换、无需重启或改服务权限。
- 部署器新增服务用户身份的 XLSX 解析检查；在停止任何服务前失败退出。
  检查移除 root 的 Python 路径覆盖，以服务用户 HOME 和真实 parser 验证文本 ID、默认加工和默认 Token。
  本机 RHEL Python 的 `PYTHONNOUSERSITE=1` 也会隐藏共享 `/usr/local` 搜索路径，
  检查不添加该标志，保持与真实服务的启动环境一致。
- 不创建发布任务、短链或测试帖，不改队列、计划、凭证及历史状态。

## GitHub-first 操作

先提交并推送修复，再在 CPU 43.166.187.96 检出精确提交。只执行依赖补齐，
不要重跑昨天的 41 文件全量部署。挂载必须为已验证的数据盘 UUID。
发布目录记为 RELEASE；下载和安装输出放数据盘，pip 禁用缓存。

```bash
python3 -m pip download --only-binary=:all: --no-deps --require-hashes --no-cache-dir \
  -r "$RELEASE/requirements-fb-excel.txt" -d "$BACKUP/wheels"
python3 -m pip install --no-index --find-links "$BACKUP/wheels" --no-deps \
  --require-hashes --no-compile --target /usr/local/lib/python3.9/site-packages \
  -r "$RELEASE/requirements-fb-excel.txt"
runuser -u fb-auto-post -- /usr/bin/python3 "$RELEASE/scripts/check_fb_excel_runtime.py" /opt/fb-auto-post/current
```

验证实际服务用户、存量进程 HTTP 预检和浏览器“上传并校验”；仅停留在预览，
不点击确认发布。确认 worker、sidecar、主 API 与调度器 PID 未变。

## 回滚边界

四个新增目录为 `openpyxl`、`openpyxl-3.1.5.dist-info`、`et_xmlfile`、
`et_xmlfile-2.0.0.dist-info`。部署证据记录完整文件哈希；回滚前核对目标根目录、
没有依赖漂移，将这四个目录移动到此次备份的 `disabled/`，保留可恢复副本。
禁止删除 root 私有包、其他 Python 包或恢复旧业务数据库。
回滚仅撤销本次新增依赖，会重新导致 Excel 解析不可用；已加载模块的现存进程
仍可能继续使用缓存。若必须关闭入口，使用原 Excel admission gate 的维护流程。

## 验证结果

- 修复提交 `e6f040f24ce04eee8d586e21648c8907eb5ede9f`；RHEL 身份检查兼容修订
  `15ae6e87685bf503fb412eed65d5eab4ab80bf0f`，均已 GitHub 推送并在 CPU 精确检出。
  检查器 release 为 `/mnt/data-disk/fb-video-publish/releases/15ae6e8`；实际业务代码目录
  `/opt/fb-auto-post/current` 保持不变，只新增共享 Python 依赖。
- 备份/安装清单：`/mnt/data-disk/fb-video-publish/backups/excel-runtime-20261022-e6f040f/manifest.json`。
  四个目标原先均不存在；209 个新增包文件 SHA256、安装 wheel 和服务前后快照已保留。
- 2026-10-22 **11:21:02 北京时间**，对存量 PID 2771323 的实际
  `POST /api/fb-video-publish/excel/preview` 请求返回 **HTTP 200**：
  `total=1, valid=1, skipped=0`。既有已发布素材仍提示历史重复，不执行发布。
  验证使用已保存任务的可信身份快照；内部认证只在进程内使用，证据不包含 bearer 或预检令牌。
- `operation_created=false`、`submitted=false`；主 API、sidecar、worker、调度器 PID
  及全部 FB timer 状态均未变。未执行重启、队列变更、数据库恢复或发布。
- 只读验收证据：`/mnt/data-disk/fb-video-publish/evidence/excel-runtime-20261022.json`。
- 本地 53 项检查通过（解析/预检 26、部署/回滚 24、服务身份 3）；修订检查器后
  再次通过 3 项身份检查及线上服务用户实际 XLSX 解析。
- 已登录浏览器打开 Page 管理及 Excel 弹窗正常；扩展未获本地文件 URL 访问权限，
  自动文件选择被阻止，因此**未声称浏览器上传验收通过**。没有更改浏览器权限，
  最终以真实运行中的 HTTP 预检为恢复证据。

需要撤销依赖时，先核对 manifest 的 `installed_sha256` 与四个固定目录一致（忽略
`__pycache__`），确认无其他版本/调用方变更，再按以下步骤保留副本：

```bash
BACKUP=/mnt/data-disk/fb-video-publish/backups/excel-runtime-20261022-e6f040f
mkdir "$BACKUP/disabled"
mv -- /usr/local/lib/python3.9/site-packages/openpyxl \
  /usr/local/lib/python3.9/site-packages/openpyxl-3.1.5.dist-info \
  /usr/local/lib/python3.9/site-packages/et_xmlfile \
  /usr/local/lib/python3.9/site-packages/et_xmlfile-2.0.0.dist-info "$BACKUP/disabled/"
```

该命令不会更改业务库或原来的 root 私有依赖，但会重新破坏后续进程的 Excel
解析能力；正常情况下不要执行。修复不需要服务 restart/reload。
