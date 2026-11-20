# 长视频与选区字幕识别

2026-11-20，基线 `da70592fdc833504bace62cb5a84eba8bb0c1aca`。

`language_media.transcribe(path, source_language, ranges=...)` 接收源视频毫秒区间对；省略时识别全片，空列表或越界区间报错，绝不回退全片。先合并重叠/相邻区间，再按 60 秒分段。每次音频抽取最多 45 秒、Whisper 最多 120 秒；OCR 每段最多 60 秒视频、约每秒一帧（原有最多 72 帧保护）、抽帧 50 秒和识别 110 秒预算分别重置。临时媒体写入编辑器数据盘 `temp` 并在每段结束后删除。

内嵌字幕用输入 seek、绝对时间戳和输出终点读取，保留跨 seek 的字幕，再裁到所选范围。ASR/OCR 使用区间输入 seek 和输入时长，结果加回源时间。相邻重复文字只合并相接/重叠字幕；离散选区不填补间隙。ASR 跳过近乎数字静音，并剔除静音区域的幻觉及 `[BLANK_AUDIO]`。空段可以成功无字幕；引擎失败必须使整次识别失败，错误标明源区间，不返回已有片段冒充完整结果。ASR/OCR 仍是需校对的草稿，OCR 仍为抽样，跨分段的语句可能需校对。

`studio.prepare` 先取得所有脚本与变体的 clips，按源汇总范围，一次识别合并范围，最后按各成片的 clips 顺序映射到输出时间轴。同脚本变体复用 composition。翻译仍每批最多 100 段，并增加同时遵守既有 12000 字上限的分批；配音、权限、素材归属/指纹/安全路径及独立渲染服务保持原有约束。

工作台仍使用已有持久化 studio_runs、30 秒心跳和 180 秒租约，不持有制作 HTTP 连接。兼容同步 `/language/generate`；字幕工具可用 `{mode:"transcribe",async:true,task_id,source_language}` 获得 202 任务回执，再 GET `/language/transcripts/{id}`。后台独立编辑器内的字幕消费者使用持久化 owner/task/language 唯一任务、30 秒心跳和 180 秒租约；重复提交复用进行中/完成回执，失败只在用户再次提交后重试。没有新增 cron 或飞书发送。

## 验证

- `test_ai_video_editing_transcript.py`：长片 ASR 分段、后部/离散/重叠区间、末段、边界重复合并、静音、失败不返回部分成功；真实 FFmpeg 17:52 内嵌字幕的全片和后部 seek；工作台多变体映射和复用；后台字幕任务状态/归属/进度/失败。
- 既有 `test_ai_video_editing_studio.py`（设置 CPU 字幕保护 Python）与 `test_ai_video_editing_smart.py`。
- `test_editor_transcript_deploy.py`：有在途工作必须解冻原进程且不能终止或切换；并发版本不能覆盖。
- 本机隔离媒体实测：真实 Whisper 17:52/18 段整片及后部 6/11/3 秒区间；真实 Tesseract 后部离散区间、62 秒边界和 17:52 整片。结果在 repo 外的交接 evidence 保存，未提交私有媒体/凭据。

## GitHub 后窄部署

先 push，再在独立目录从 GitHub fetch 该确切 SHA 并 detached checkout。仅用该 checkout 的本脚本：

```sh
python3 ops/editor-transcript/deploy.py install --repo "$DEPLOY_CHECKOUT" --base da70592fdc833504bace62cb5a84eba8bb0c1aca --version "$COMMIT"
```

脚本核验磁盘 UUID、干净 checkout、远程分支包含提交、当前基线和受影响文件内容。复制当前发布包保留并发未改文件，只替换本次模块和 `video-editing-ai-language.js`；备份代码、配置、SQLite 一致快照到 `/mnt/data-disk/editor-transcript-deploy/backups/`。不修改主后台源码、GPU、调度、nav 和 nginx。

旧版编辑器没有 graceful drain：发布包准备和备份后，短暂 SIGSTOP 该编辑器主进程，检查其 CPU 任务（含过期但未核实的活动记录）、子进程、已建立 HTTP 请求，存在任何工作立即 SIGCONT 恢复且不切换。确认为空闲才原子切链接并排队 TERM、CONT，让旧进程在再次执行应用线程之前退出，再只启动 `ai-video-editing.service`；从不使用 SIGKILL、不超时清锁、不等待无关 GPU 渲染。新版本亦采用这一窄范围空闲切换方式。

回滚使用 manifest，只换代码/必要静态文件，不恢复数据库或任何业务事实：

```sh
python3 /ABSOLUTE_RELEASE_CHECKOUT/ops/editor-transcript/deploy.py rollback --manifest /ABSOLUTE_BACKUP/manifest.json
```

技能上下文已记录于此 GitHub 文档；本次不改共享技能已发布目录，避免影响其他并发任务。
