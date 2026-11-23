# GPU 成片上传与 COS 播放

原成片之外，GPU 额外制作最大 480×854、视频峰值 900 kbps、音频 64 kbps 的流畅预览，并分别上传 COS。预览/下载采用独立摘要和对象回执；默认播放轻量版本，可切换原画质，下载始终返回原成片。预览生成失败时保留可用原成片，不将成功作品标为失败。

GPU 保留原有 NVENC 制作、租约与 pending.json。成片先上传香港 COS 私有对象，GPU 通过 SDK MD5 校验和 SHA-256 固定对象键；CPU 仅接收元数据，HEAD 核对大小、任务和摘要后完成任务。丢失上传/完成回执先核对原对象及原任务，不重做成片。

播放、下载仍经过原用户/租户鉴权，随后 307 到一小时 COS 签名 URL，保留 Range 与下载文件名。旧 CPU 文件继续可读。字幕/批量 ZIP 只在显式使用时缓存云端文件；正式 YouTube 素材入库使用 COS 服务端复制，保留原入库授权及回执。没有新增公共读权限。

GPU 依赖：隔离目录安装 `cos-python-sdk-v5==1.9.44`；专用 EnvironmentFile 仅保存已有 GPU 的 `COS_BUCKET/COS_REGION/COS_SECRET_ID/COS_SECRET_KEY`，配置必须与 CPU 同一个香港桶。仅给独立渲染单元添加该文件及隔离 PYTHONPATH。

先推送 GitHub，双端获取相同 commit，再 CPU、GPU 顺序安装：

```sh
python3 CHECKOUT/ops/editor-gpu-cos/deploy.py install --role cpu --repo CHECKOUT --base CPU_BASE --version SHA
python3.9 CHECKOUT/ops/editor-gpu-cos/deploy.py install --role gpu --repo CHECKOUT --base GPU_BASE --version SHA
```

脚本比对本次文件实际基线，备份 SQLite（仅用于审计）、保留旧 release，仅在受影响进程原子空闲检查成功后切换。忙时保留进程与 prepared manifest，用 `resume --manifest MANIFEST` 重试；不强杀工作、不等待无关服务。

现有单条成片迁移在 GPU 执行：

```sh
python3.9 CHECKOUT/ops/editor-gpu-cos/migrate_output.py --task TASK --work /data/ai-video-editing/gpu-cos/migration
```

先验证 CPU 原文件 SHA，再由 GPU 上传 COS、核对回执，最后更新同一任务的存储指针；不改时长、素材、任务身份或原文件。

回滚写入链路：`python3.9 CHECKOUT/ops/editor-gpu-cos/deploy.py rollback --manifest GPU_MANIFEST`。旧 worker 恢复 CPU 文件回传；保留新的 CPU COS 读取兼容层及所有数据，云端成片继续可用。不得恢复旧数据库或删掉已完成对象。GPU 额外依赖/凭据环境可保留待复用，不影响旧 worker。
