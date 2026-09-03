# YouTube 香港媒体执行迁移

目标：将 COS 下载、SHA/ffprobe 校验、YouTube resumable 字节上传放到香港 GPU `43.154.250.89`。CPU `43.166.187.96` 继续唯一持有页面/API、SQLite 队列、OAuth、频道校验、评论、状态核对和统一表同步。

验收：香港目录固定 `/data/drama-synthesis-gpu/work/youtube-publish`；仅走既有 loopback SSH 隧道与 Bearer；GPU 不保存 OAuth/数据库凭据；远端文件必须匹配冻结 SHA/size；不静默回退 CPU；不创建测试发布，以离线测试、health 和队列守恒验收。
