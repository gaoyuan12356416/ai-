# YouTube 普通视频发布

剪辑端使用独立编辑服务和香港渲染 worker。发布端使用现有素材库、封面准备流程、频道授权与 YouTube 发布账本。定稿的素材 ID、文件大小、SHA-256 和剪辑版本随普通视频准备任务保存。操作人确认后，发布账本首次写入就包含已审核的文件指纹。独立上传引擎和线上 canonical worker 复核下载的实际字节。

## 发布

将两个已经推送的 GitHub 提交分别检出到 CPU、香港的独立工作树。以下变量分别指向两个检出目录及其完整提交 SHA，`RUN` 为发布端工作树内的 `ops/youtube-regular/deploy.py`。CPU 使用 `/mnt/data-disk/youtube-regular-deploy`，香港使用 `/data/ai-video-editing/youtube-regular-deploy`。

先在香港用新版本运行普通视频 FFmpeg 测试，再依次执行以下命令。每条命令在对应主机执行。

```sh
/usr/bin/python3.9 "$RUN" install editor-gpu "$EDITOR_REPO" "$EDITOR_SHA"
python3 "$RUN" install editor-cpu "$EDITOR_REPO" "$EDITOR_SHA"
python3 "$RUN" install publisher "$PUBLISH_REPO" "$PUBLISH_SHA"
```

安装器为现有代码、静态文件和 SQLite 留存备份。它检查本次更改的文件，保留宿主复合安装中的其他版本。编辑服务的旧 release 和香港旧渲染 release 继续保留。YouTube 准备 worker 与香港渲染 worker 只向主进程发送退出信号，当前工作结束后由 systemd 启动新进程。主 API 通过已有 ExecStop 排空在途操作，切换完按原快照恢复 YouTube 调度。检查 manifest、调度恢复报告、静态资源和作者权限接口。运营在正常确认页发出业务视频。

## 回滚

先处理仅新版本能继续的普通视频待审、待定稿和入库对账项。已写入正式发布账本的任务携带独立指纹，按原回执继续推进。按发布端、CPU 编辑端、香港渲染端的顺序回滚。已确认的素材、公开链接、定时预约、上传回执和当前数据库继续保留。

```sh
python3 "$RUN" rollback publisher "$PUBLISH_REPO" "$PUBLISH_SHA"
python3 "$RUN" rollback editor-cpu "$EDITOR_REPO" "$EDITOR_SHA"
/usr/bin/python3.9 "$RUN" rollback editor-gpu "$EDITOR_REPO" "$EDITOR_SHA"
```

每个 scope 的 `backups/<scope>/<SHA>/manifest.json` 记录原版本、目标文件和进程切换。若后续部署修改了相同文件，依据 manifest 合并回退本次差异。若已有另一份编辑 release 生效，使用其版本记录继续核对。

发布详情的公开链接可作为静态更新单独上线。当已经运行上一个发布端提交时，在 CPU 上从新的 GitHub 工作树执行 `python3 "$RUN" install publisher-ui "$UI_REPO" "$UI_SHA"`。回退整套功能时，先用相同参数将 `install` 换为 `rollback`，再回退发布服务。静态更新独立备份页面和脚本，两份页面均沿用线上导航版本，无需切换进程。

发布端已有普通视频能力时，后续跨页面和服务的增量更新可在 `install publisher` 命令末尾加 `--from-version "$PREVIOUS_SHA"`。该 SHA 必须是当前生产版本对应的 GitHub 祖先提交。安装按变更文件核对线上内容并备份，回滚从本次 manifest 读取前一版本。多次上线按实际顺序倒序回退。
