# BUG-006：X Auto 首次真实运行缺少 ffprobe 路径

状态：已定位，待完成生产修复验收。

## 现象与影响

- 2026-08-12 18:14:35，操作员创建手动 Run `1`；18:15:16 显示失败。
- task `1` 的稳定错误码为 `media_probe_failed`，消息明确指出
  `/usr/bin/ffprobe` 不存在。
- 失败发生在 X 写入前：canonical auto-template run `9` 为 `failed_preflight`，
  queued/published/unknown 均为 0；没有 canonical queue、publish log 或 Post。
- material `6120551` 的临时预留已按事件链原子释放，X Auto 永久 ledger 为空；原
  Run/bridge run 必须保留，不得删除或重放。

## 根因

生产已有经既有 X 发布链路使用的只读静态二进制：

`/mnt/data-disk/x-post-automation/bin/ffprobe`

其 SHA256 为
`4f231a1960d83e403d08f7971e271707bec278a9ae18e21b8b5b03186668450d`，
权限为 `root:root 0555`。`x-post-daily` 用户和与 X Auto 相同的 systemd 沙箱都能
执行。现有 daily/sidecar 环境已声明 `X_POST_FFPROBE_BIN`，但 X Auto 为隔离 bearer
而不加载 `/etc/x-post-daily.env`，自己的 `/etc/x-auto-post.env` 又漏了该非密钥项，
所以公共探测函数回退到了主机上不存在的 `/usr/bin/ffprobe`。

## 修复

- 在 X Auto 独立非密钥环境模板与生产环境中显式设置共享静态探测器路径。
- unit 增加 `ExecStartPre=/usr/bin/test -x .../ffprobe`，依赖缺失时 sidecar 启动
  失败关闭，避免再由真实任务发现。
- 不修改 X 发布 API、媒体门禁、账号会员判断、队列/日志或失败 Run。

## 验收边界

- 本地和 exact server release 跑 X Auto 聚焦回归。
- 用服务用户及等价 unit 沙箱对离线短视频执行真实 ffprobe 解析。
- 只读 preview 必须成功且 `reserved=false`；自然 timer 成功。
- 不调用 Run `1`、`run-now`、publish 或 X canary；X queue/log/Post/unknown 不得因
  部署验收增加。
