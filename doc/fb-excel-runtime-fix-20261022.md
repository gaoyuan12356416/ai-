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

部署后补充实际提交、目录、浏览器预览结果和备份位置。
