# 视频剪辑紧凑导出

长片使用原 `720×1280 / 30 fps / H.264` 规格，但旧默认 `NVENC p4 / CQ21` 在最终导出时会产生偏高码率。此次为新制作任务冻结 `video-editing-720-v2`，只改变最终输出编码；中间片段及历史 v1 任务保持原质量参数、时间轴、转场和音频规则。

GPU 同时读取 v1/v2，CPU 新建 render 默认 v2，素材 probe 继续 v1。`VIDEO_EDITING_EXPORT_PROFILE=video-editing-720-v1` 可把后续新任务恢复为旧参数，既有任务继续按冻结 profile 执行。不得直接把共享 `recipe.PROFILE` 改回 v1。

本次既有成片只生成独立压缩副本。保留原私有成片、原公开入库副本、任务/output 的 hash 和已经失败的素材上传请求。压缩不解决素材接口对产品 3214 的业务 403，不自动再次提交入库。

## 发布源与边界

- feature 分支：`codex/video-editing-compact-export-20261029`。
- GPU composite 分支：`codex/video-editing-compact-gpu-20261029`，基于现场运行的 `a0c5cd914472e7c80422ee0ef8d1a356cb7ea64c`。
- GPU composite 仅带入 feature 的 `features/video_editing/{recipe,render,gpu_integration}.py`，可带入本次 installer/doc。共享三路 probe、单路 render 运行时不变。
- CPU 是复合安装，只更换 `features/video_editing/{recipe,service}.py`。installer 固定两文件现场 SHA，出现偏差必须先合并实际改动，不能整目录覆盖。
- `scripts/deploy_video_editing_compact_export.py` 默认 `--phase check`，仅查看，不创建标记、不停止服务、不改配置。
- 两个 release checkout 均须 clean，HEAD 与各自 GitHub 分支完全一致。GPU 三文件须与 feature checkout 字节一致，且相对 a0c 不包含其他代码改动。

## 构建和验证

先完成 feature 提交并 push，然后从 a0c 新建 GPU composite checkout。只复制上列三个 GPU 文件和本次 installer/doc，提交并 push GPU 分支。不得把 CPU feature 整棵树直接替换 GPU release。

使用实际已推送的 40 位提交替换下面 `FEATURE_SHA` 和 `GPU_SHA`。在 CPU 数据盘独立 stage 和 GPU `/data` 下的独立 stage 拉取 feature。GPU composite checkout 必须为 `/data/drama-synthesis-gpu/releases/GPU_SHA`。所有 git checkout 均来自 GitHub；不要从本机直传生产源代码。

CPU 已保存的 GitHub 身份：

```sh
export GIT_SSH_COMMAND='ssh -i /root/.ssh/github_codex_cpu_ed25519 -o IdentitiesOnly=yes -o BatchMode=yes'
git clone --no-checkout git@github.com:gaoyuan12356416/ai-.git /mnt/data-disk/video-editing/releases/FEATURE_SHA
git -C /mnt/data-disk/video-editing/releases/FEATURE_SHA fetch origin codex/video-editing-compact-export-20261029
git -C /mnt/data-disk/video-editing/releases/FEATURE_SHA checkout --detach FEATURE_SHA
```

GPU 使用该主机现有已验证的 GitHub 身份，先 `git ls-remote` 验证 repo 读取；不要复制 CPU 私钥。feature stage 可位于 `/data/drama-synthesis-gpu/validation/compact-feature-FEATURE_SHA`。GPU composite 拉取与 CPU 同样的 GitHub repo，但 checkout `GPU_SHA`。

本地/候选验证：

```sh
python -m py_compile scripts/deploy_video_editing_compact_export.py
python -m unittest discover -s tests -p 'test_video_editing_*.py'
git diff --check
```

实际 FFmpeg 验证应包括一片段最终输出、13 片段多层拼接、fade、完整音画时长以及 v1/v2 恢复隔离。GPU installer 还会按服务用户运行既有 `check_drama_synthesis_gpu_runtime.py --check-app-import`，使用当前环境而不打印凭据。

## 部署顺序

在两个主机都从 feature stage 执行 installer；`--gpu-release` 指向另外的 GPU composite checkout。

1. CPU 只读检查：

```sh
python3 scripts/deploy_video_editing_compact_export.py --host cpu
```

2. GPU 只读检查：

```sh
/data/drama-synthesis-gpu/runtime/current/bin/python scripts/deploy_video_editing_compact_export.py --host gpu --gpu-release /data/drama-synthesis-gpu/releases/GPU_SHA
```

3. CPU 关闭新的剪辑入口，等待现有任务结束，再暂停两个 GPU claim worker。默认最多等 45 秒；有正在制作的任务时可分次检查或显式传合适的 `--drain-timeout`，不能杀任务来满足检查。

```sh
python3 scripts/deploy_video_editing_compact_export.py --host cpu --phase prepare
```

此阶段备份路径为 `/mnt/data-disk/video-editing/backups/compact-export-FEATURE_SHA前12位`。记录 claims 原状态、未涉及服务 PID 和 task/output/material-upload 表摘要。新的剪辑入口关闭，已接收任务继续完成。

4. GPU 排空共享任务及当前 YouTube 媒体请求，完成预检、备份和原子 release 切换，仅重启共享 GPU worker。CPU claims 此时仍暂停，所以不会在切换间隙投递新任务。

```sh
/data/drama-synthesis-gpu/runtime/current/bin/python scripts/deploy_video_editing_compact_export.py --host gpu --phase apply --gpu-release /data/drama-synthesis-gpu/releases/GPU_SHA
```

GPU 备份为 `/data/drama-synthesis-gpu/deploy-backups/compact-export-GPU_SHA前12位`。核对所有持久 GPU 回执 hash 未变化、共享并发仍为 probe 3/render 1、隧道可用。现有 `/healthz` 的 `video_editing_profile` 字符串在未改动的共享 worker 中硬编码为 v1；保持该字段原值是预期，实际 v2 支持由候选源文件和执行测试确认。

5. CPU 安装两个文件，短暂停启 API 使默认 v2 生效：

```sh
python3 scripts/deploy_video_editing_compact_export.py --host cpu --phase apply
```

此阶段保存旧两文件和在线 SQLite 证据备份。数据库备份仅作证据，任何恢复都不得覆盖线上 SQLite。验证匿名 `/api/video-editing/config` 仍返回 401，三张业务表摘要保持不变。登录读取 config 应显示 `video-editing-720-v2`，除非已有显式 v1 环境覆盖。

6. 恢复原先运行的 claims 并打开 CPU 剪辑入口：

```sh
python3 scripts/deploy_video_editing_compact_export.py --host cpu --phase restore
```

全程不停止 YouTube publisher 或素材入库 worker；installer 核对这些进程 PID 保持不变。不调用素材入库 POST，不恢复旧失败上传，不改原视频对象。

## 失败处理与回滚

prepare 阶段尚未安装文件时，可用 `--host cpu --phase abort` 恢复 claims 和剪辑入口。出现 GPU/CPU 安装错误后，保留标记、备份和当前业务账本，先核对已经执行到哪一步。禁止为了重跑而删除备份或修改旧回执。

**一旦任何 v2 任务写入 CPU 或 GPU 账本，包括 completed，不能把 GPU 切回 a0c，也不能恢复 CPU 的旧 recipe.py。** 旧运行时启动时会校验所有历史 payload，遇到 v2 可能导致共享 worker 无法启动。

正常回滚只恢复后续新任务的默认质量：

```sh
python3 scripts/deploy_video_editing_compact_export.py --host cpu --phase rollback-default
```

它保留 CPU/GPU 双 profile 读取与编码代码，在 `/etc/video-editing/video-editing.env` 写入 `VIDEO_EDITING_EXPORT_PROFILE=video-editing-720-v1`，排空剪辑后只重启 API 和剪辑 worker。已有 v2 任务、结果、COS 对象、上传请求和所有历史记录保持不变。配置原件及回滚证据保存在 `compact-default-v1-UTC时间` 备份目录。

只有明确证明 CPU 与 GPU 两边均没有任何 v2 记录时，才允许完整回到旧代码；必须在入口关闭、claims 暂停、GPU 排空状态下逐文件恢复 CPU 两文件，并恢复 GPU release link/`DRAMA_GPU_RELEASE_SHA`，保留在线数据库。若不能完整证明，使用上述默认参数回滚。
