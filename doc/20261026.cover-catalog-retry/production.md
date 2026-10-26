# 生产验收与回滚

2026-10-26 北京时间，CPU 43.166.187.96。

- GitHub 运行版本：`155c697f243b57daf7c0e5db8227112121a51a02`，分支 `codex/cover-catalog-retry-20261026`；生产从服务器已 fetch 的 Git 对象解包。
- 安装文件：`/root/drama_material_service/features/cover_material_source.py`，SHA256 `53574c98d5ae472c86c22baf28a6011346e03378c7a5e685fbf33af932fb5838`。Windows 工作区使用 CRLF，验证部署使用 Git blob/Linux release 字节。
- 主 API 在 11:23:13 窄重启，PID 225570 → 962891。四个图片生成实例 PID 225547/225548/225555/225553 均未变化；组合 app.py SHA 保持不变。
- 本地和 CPU 均通过 66 项上传契约测试、4 项精确任务恢复测试。恢复测试包含并发修改时禁止覆盖、回调失败不标记完成、原图片和 token 计数保留。

## 目标恢复

任务 `e2a511bd8a664b6b8279e7c3a42f42d8`，moboreels / 3360 / 132516419：

| 尺寸 | 正式素材 ID | 出站次数 | 本次处理 |
|---|---:|---:|---|
| 1:1 | 7835627 | 1 | 复用已有成功回执 |
| 1.91:1 | 7835640 | 1 | 复用已有成功回执 |
| 4:5 | 7835858 | 1 | 对账确认尚未提交后补交一次 |

三个素材均按签名只读回查，验证产品、剧目、图片类型、语言、尺寸、人员和正式 ID。
公网图片在恢复前后 GET、解码、比例和 SHA256 检查通过，三张图片字节未变。
本地 8787 和公开 HTTPS 的带授权任务详情都返回 HTTP 200、done、100%；已清除该任务错误。
重启前快照中的 14 个其他未完成任务均保留，已有 URL 不变，后续制作交现有后台 worker。
验收时全局队列为 1 processing_cover / 13 queued，仅为快照，不声称其他任务全部完成。
启动日志中另有 social_analytics 报表客户端断开连接的 BrokenPipe；它不在本次改动模块中，
目标截图回调及详情读回正常，本次未扩展修改该报表。

## 证据与备份

`/mnt/data-disk/codex/tasks/cover-catalog-retry-20261026/`：

- `before.json`：原失败任务、原三条上传账本、图片校验信息。
- `source-before.sqlite3`、`uploads-before.sqlite3`：SQLite 在线备份，quick_check=ok。
- `callback-result.json`、`recovered.json`：唯一新增提交、三份回执、图片不变及完成状态。
- `deployment.json`：Git 版本、文件哈希、前后 PID、队列保留、公开和本地 API 验收、正式素材回查。
- `cover_material_source.before.py`：旧模块，SHA256 `ea15d3e3776e3c5f966a805d0901ef79d148ab6bf768827649bb5cf09c557bb1`。

## 仅代码回滚

在上述 CPU 主机执行以下命令。新版本若已有后续修改，哈希守卫会停止。
保留所有任务、图片和上传账本，绝不恢复 SQLite，也不重新调用已完成任务的 remake/retry。

```bash
python3 - <<'PY'
from pathlib import Path
import hashlib, os, shutil
target = Path('/root/drama_material_service/features/cover_material_source.py')
backup = Path('/mnt/data-disk/codex/tasks/cover-catalog-retry-20261026/cover_material_source.before.py')
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(target) == '53574c98d5ae472c86c22baf28a6011346e03378c7a5e685fbf33af932fb5838'
assert sha(backup) == 'ea15d3e3776e3c5f966a805d0901ef79d148ab6bf768827649bb5cf09c557bb1'
temp = target.with_name('cover_material_source.rollback.tmp')
assert not temp.exists()
shutil.copy2(backup, temp)
os.replace(temp, target)
PY
# 仅在上一步成功后执行：
systemctl restart drama-material-api.service
systemctl is-active drama-material-api.service
```

此次维护同步更新 AI 后台维护技能的封面数据库断线恢复规则；没有修改 Codex memory 文件。
