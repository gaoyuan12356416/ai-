# 验证报告

## 本地实现验证

- Drama：361 tests OK，6 项既有 COS SDK 环境测试跳过；新增 20 项全部通过。
- TT：最终 138 tests OK；Intel OpenCL 旧新 shader 实测通过，旧输出字节相同；3 组新参数每组 90 个通道误差小于 1/255。
- FB：最终 38 tests OK；真实 FFmpeg 旧/新 × 有声/无声共 4 次渲染，单音轨且音频 PCM 一致。
- 集成独立复核：110 tests OK；实际 Drama legacy adapter 再做 4 次真实 FFmpeg 渲染通过。
- 后台两个 inline JavaScript 块 node --check 通过；Python 编译和 diff whitespace 检查通过。

## 独立 release 的真实 GPU 验收

- Drama `223577b41466d03fd330625441ab3b7c1d86e843`；TT `80acd8b4f082e121f8332dea2a34f23d739b1bb3`；FB `96dc90d1d59622098fa36493862e37e485b84545`，均已推送 GitHub 并按精确提交检出。
- 9 个真实 GPU 输出全部完整解码通过：Drama 60 秒片（1800 帧）；Drama 历史、最低值、最高值 OpenCL；TT 历史/新配方；FB 历史/新配方；原停住源的完整 108.3 秒重现片（3249 帧）。
- 全部 720×1280 / 30fps / 单音轨，Drama、FB 为 H.264，TT 为 HEVC；媒体合同不变。
- TT 竖版、横版、分辨率变化三组各 60 帧（2 秒），延迟组 61 帧（61/30 秒）的源帧时间轴比较，mismatch 均为 0。
- CUDA、OpenCL 离线预检各验证 80 个不可变 RGBA 缓存。最终 Drama 提交仅改变旧 FB 适配器的音频结束参数；离线验收逐文件核对原生模块、内核、worker、预检代码与先前已通过版本完全一致后复用该缓存预检证据，同时验证最终 release 身份和导入。随后正式生产启动另行完成了最终提交的完整预检，见下方时间记录。
- 独立验收没有上传或发布业务素材。报告：HK `/data/drama-synthesis-gpu/work/source-overlay-qa-20260920/report.json`；本地 `output/random-source-overlay-release-20260920/gpu-acceptance.json`。
- 60 秒验收视频 SHA256：`9d7a07c45ee4ee984128c9077a992fff533dc564aa4fb5fe8681b53662eaa469`，本地复制后散列一致并抽帧检查。

## 生产切换

2026-09-20 17:43:35–17:45:47（北京时间）完成最终 Drama 启动预检，80 个缓存验证通过，ExecStartPre 退出码 0。全部在途旧渲染先自然完成，三个 GPU 服务均以新提交运行：

| 链路 | 新进程 PID | 运行版本 | 线上结果 |
| --- | --- | --- | --- |
| Drama | 2825393 | `223577b41466d03fd330625441ab3b7c1d86e843` | `source_overlay_version=1`，CUDA 管线、v4 fullbleed |
| TT | 2825400 | `80acd8b4f082e121f8332dea2a34f23d739b1bb3` | `source_overlay_version=1`，HEVC v3 |
| FB | 2825402 | `96dc90d1d59622098fa36493862e37e485b84545` | `source_overlay_version=1`，H.264 v3 |

- 每个进程的 `/proc/<pid>/cwd`、current 指针、Git HEAD 和保留的 catalog SHA 已逐项相符；三个反向隧道全部重启并从 CPU 18788/18830/18836 验证成功。
- CPU allowlist 原子更新成功，API PID 3608346、worker PID 3608348 均 active；公共页面和两份 index.html 相同，UI SHA256 `c1cb017e1d0995af629691d2c60cdc6c96eadfb1e76e1b61fb235885d51dae17`。
- 从实际部署的 CPU core 生成内存审计配方：透明度 2.69%、放大 136.91%，重复生成完全相同；无字段历史配方仍合法。没有为审计创建业务任务。
- TT 原 4 个 active timer 已恢复，原 3 个 inactive path 保持；TT maintenance gate 移除；FB prepare.timer 恢复 active。
- 常规 FB PID 3465403 和运维 prepare-only PID 3561822 均已恢复，确认不再 T；运维 X prepare-only PID 3558700 已自然结束。并行任务曾恢复被暂停进程，经任务间协调再次暂停，切换后已通知原任务恢复证据。未新增定时检查、未替并行任务重做素材或重播发布。
- 自然新收据已经落盘：TT `ttauto-1777-6af8410bbe4604f262da428dc098ba700c1e` = 2.79% / 111.15%；FB `fb-page-4fc684e5182b6737cbb7c230154160d8f3fefcff44feb8ba` = 3.51% / 123.14%，另一新 FB 收据为 4.02% / 128.84%。这是新任务冻结配方已生效证据，不能等同于这些长视频已经制作或发布成功。
- 最终审计 `ok=true`：CPU `/mnt/data-disk/random-overlay-gpu/backups/20260920-source-overlay/final-audit.json`；HK `/data/random-overlay-gpu/backups/20260920-source-overlay/switch-after.json`。本目录 `evidence/` 保存脱敏的本地副本与 GPU 验收报告。
- 17:49:12 复核：三路 GPU 均 active、NRestarts=0，启动后的服务日志无 ERROR/Traceback；TT/FB 后续新收据参数均在范围内，`evidence/post-release-audit.json` 为 ok=true。维护 skill 的 `references/project-map.md` 已同步本次规则、版本、收据语义、卡住修复和回滚限制。
