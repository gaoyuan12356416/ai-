# YouTube 公开状态延迟修复

公开设置请求成功后，立即读取仍可能暂未确认 public。旧引擎仅读取一次便终止为 youtube_public_readback_failed；2026-09-11 两条实际已公开视频因此仍显示发布失败。后续同频道实时读取均为 public / processed / succeeded，沿用原任务恢复后各自上传计数仍为 1，已配置首评的任务仅发送 1 次。

## 行为

- 仅修改 reviewed_thumbnail 引擎。公开请求之后最多读取 4 次，间隔 2、5、10 秒；每次续租并校验原视频和原频道。
- HTTP 200 与写入超时均执行相同的只读确认。不重复公开请求、上传、封面或首评。
- 暂时网络/5xx读取失败可在此有限次数内重查。视频消失、身份冲突、异常隐私不继续轮询；处理失败保留真实失败。
- 多次仍未确认则记为 youtube_public_readback_unknown，保留原 ID，暂停首评与自动执行。用户显式重试沿用原视频；先读回已公开状态时不再 PUT。
- 首评前再次确认公开和处理成功，未知首评仍禁止重复。

## 验证与部署

`python scripts/test_youtube_auto_engine.py`：46 项通过，涵盖状态传播延迟、临时查询失败、空视频结果、持续私享、写入未知、重复上传和首评栅栏。

部署只安装 `features/youtube_auto_publish/engine.py`，精确匹配旧文件 SHA，先备份，再原子替换。仅向 auto worker 主进程发送 SIGTERM，由原有处理循环自然结束并重启；不修改 API、旧 worker、统一 writer、前端或生成器。

GitHub 精确归档需写入 `.github-verified-commit`，先运行 `scripts/deploy_youtube_public_readback.py --commit <SHA> --check`，再执行同命令去掉 `--check`。

回滚：`python3 <release>/scripts/deploy_youtube_public_readback.py --rollback <backup>`。脚本拒绝覆盖后续文件漂移，保留 SQLite、通知 outbox、视频/首评和图片事实；不得用恢复前数据库覆盖已经完成的真实发布。

频道权限故障独立处理：403 forbidden 的封面失败不能靠此补丁解决；频道需要管理员核验自定义封面和长视频资格。videos.list 空结果不证明视频已删除，保留未知状态。
