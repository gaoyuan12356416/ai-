# 部署文档

## 变更内容

- TT auto CPU：模板枚举、双 client 路由、health 摘要、页面与测试。
- GPU：新增独立 direct-outro service 与独立 reverse tunnel；不重启/改写现有 random service。

## 配置项

CPU `/etc/tt-auto-post.env`：

```text
TT_AUTO_POST_DIRECT_OUTRO_GPU_URL=http://127.0.0.1:18834
```

GPU `/etc/tt-post-gpu-direct-outro.env`（非秘密覆盖文件）：

```text
TT_POST_GPU_PORT=8832
TT_POST_GPU_MEDIA_MODE=direct_outro
TT_POST_GPU_WORK_ROOT=/data/tt-post-publisher/direct-outro-work
TT_POST_DEFAULT_SOURCE_TRIM_TAIL_SECONDS=4.333333
```

内部 token、seal key、COS 密钥继续只从现有 root-only secret 文件读取，不复制到 GitHub。

## 数据库变更

无 DDL、无历史 backfill。上线前对
`/mnt/data-disk/tt-auto-post-publisher/tt-auto-post.sqlite3` 做 SQLite online backup。

## 部署步骤

1. 确认 CPU `/mnt/data-disk` 与 GPU `/data` 为真实挂载并有空间。
2. 记录 CPU/GPU current release、相关 PID、端口、unit/env SHA、自动库 integrity 与事实计数；
   确认 auto in-flight 为 0。
3. GitHub 推送 exact commit；CPU/GPU 各建立不可变 release。
4. 备份现有 auto static/source/env/unit/SQLite；备份 GPU unit/env/current 与隧道配置。
5. GPU 安装 direct-outro override env、新 service、新 tunnel，先启动 direct service并验证 8832 health，
   再启动 18834 tunnel；现有 8830/18830 PID必须不变。
6. CPU 配置 direct URL，切换 TT auto current 到 exact commit，运行编译/测试，重启
   `tt-auto-post-service.service`。
7. 发布 TT auto HTML/JS 到应用 static 与 Nginx docroot，核对 SHA。
8. 恢复/保持原 timers/path 状态，观察自然运行；不执行 run-now 或 TikTok canary。

## 验证步骤

- 两个 GPU health 的 mode/profile/asset identity。
- TT auto health 的两个路由摘要且不含 URL/秘密。
- systemd active、journal 无异常、CPU/GPU 端口精确监听。
- SQLite `PRAGMA integrity_check`、模板/版本/run/task/publish 事实计数无意外变化。
- 登录态浏览器打开模板 1：两个选项可见，历史模板选中随机排重；不保存。
- 相关全量测试、`py_compile`、`node --check`、`git diff --check` 通过。

## 回滚方案

1. 停止 auto scheduler/runner/path，确认无 in-flight。
2. CPU `current` 切回部署前 release，恢复 auto env/static 备份并重启 auto service。
3. 停止/禁用新增 direct tunnel 与 direct GPU service；删除监听由后续维护处理，不影响回滚。
4. 保持现有 random GPU service/current/env 不变。
5. 默认不恢复 SQLite；只有在新发布事实产生前且代码回滚无法解决时，停服后整体恢复 online backup。
6. 校验原 health、PID、端口、static SHA、SQLite integrity，并恢复原 timer/path 状态。

## 注意事项

- 不得通过修改现有 `/etc/tt-post-gpu.env` 在两个 mode 间切换。
- 不得把 direct worker 与 random worker 指向同一控制端口或同一工作目录。
- 不得打印 secret env 文件或凭据。
- 不得用真实 TikTok 帖子验证部署。
