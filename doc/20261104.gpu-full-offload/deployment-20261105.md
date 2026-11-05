# 2026-11-05 香港GPU部署记录

## 实际运行结果

用户于本轮明确确认“我已暂停香港 GPU 制作，可以部署”。切换前权威配置版本138：香港节点制作、发布的target/ceiling均为0，在途任务0。代理没有修改容量、重置队列或回放历史发布。

- 主机：43.154.250.89；服务：pipeline-node-hk-gpu.service。
- 运行提交：`68407465a58d99f9dd2c9a14da000f167115f804`，来自GitHub分支`codex/gpu-pipeline-full-offload-20261104`。
- 代码：`/opt/pipeline-dispatch/releases/68407465a58d99f9dd2c9a14da000f167115f804`；同SHA独立venv和staged/node.env。
- 北京时间10:32:35启动，MainPID1368842；10:36复核active/running、NRestarts0、ExecMainStatus0，实际cwd/argv/环境均正确。10:33:43已经出现新进程启动后的节点心跳。
- RANDOM_GPU_NATIVE=1、RANDOM_GPU_COMPUTE=1、RANDOM_GPU_PYTHON指向同SHAvenv；只有这三项及venv证书路径相对原环境变化。
- 保留30-rgba-asset-cache.conf，已撤下会覆盖新unit的exact99-fb-lease-receipt-20261104.conf；其租约修复代码已融合至候选。
- 制作/发布仍保持用户设置的暂停；暂停前为制作12、发布8。实际业务吞吐与整机CPU效果需恢复后观察，不能用暂停状态的CPU数字验收。

## 验证与真实边界

隔离证据目录：`/data/pipeline-dispatch/acceptance/gpu-full-offload-20261105`。

1. GitHub精确提交、干净依赖、实际pipeline-worker权限、GPU运行时及安装器encode/COS诊断通过，staged `.verified`对应运行SHA。
2. 13a74767版本10组真实验收通过：几何、实际色块、采样格式、FB30秒、X默认/限大小、TT H264/HEVC、两路各30秒并发。78项服务沙箱内相关回归通过。
3. 验收发现625MB固定素材在单进程多个profile中被重复完整GPU哈希。68407465只新增immutable素材摘要复用：PID/backend/path/dev/inode/mode/size/mtime/ctime隔离，前后fstat/lstat验证，变化重算、失败不缓存。普通源视频、下载、成片摘要未缓存。158项相关本地回归157通过、1个Windows符号链接权限跳过；Linux最终59项全部通过，含该项。真实GPU小型检查证明跨FB/TT/RGBA复用、ctime变更重算、普通文件每次计算。
4. 68407465在正式服务同等文件权限/沙箱限制下补验FB6秒、X2秒、TT HEVC2秒，全部通过。视频/音频/封装核心与13a74767逐文件字节一致，12文件及依赖指纹见`final-version-lineage.json`。
5. 完整冻结源1967.988005秒（约32分48秒）通过：源视频与输出均59039帧，H264 High/720×1280/yuv420p、AAC48k双声道，严格全片解码通过。成片1303647404字节，GPU SHA256与CPU相同：`6cb2ffc05c113e89ff09e8ce867c6bc8dfa3f42ff757da2570a6cb328cfc0966`。GPU输出摘要耗时72.985秒；不是零成本。
6. 长片在13a74767核心实现上制作，以68407465解释器验证；核心字节相同，缓存改动已单独复验。操作脚本最初错误地从容器时长推导59040帧，随后以独立源视频流59039帧/1967.966667秒作为EOF依据复核已有产物，没有重编码或放宽生产条件。音轨比视频长约0.021338秒属于原源时序。

完整长片native制作墙钟1323.238秒；其内部进程CPU阶段合计约532.75秒，不含外部音频辅助进程，不是整机CPU百分比。该轮有其他测试和生产任务并发，未做全长旧版对照，不能将其推广为生产吞吐提升比例。

完整GPU AAC候选质量仍未通过；压缩音频解码/AAC编码、异常时钟/格式的明确兼容分支、文件和网络系统调用保留CPU。当前机器没有可用GDS直传通路。12路生产负载尚未验收；已验证两路并发和完整长片。

本轮native视频接入覆盖FB、X、TT随机模板模式。TT其他模式和X普通repair保留既有实现，不计入本轮已验证的native视频迁移结果。

## 启动观察修复

第一次apply已正确写入并启动新PID，但Type=simple在子进程完成chdir/exec之前返回，立即读取cwd产生一次`runtime_cwd_mismatch`。随后核对同一PID的cwd/argv、unit、私有manifest和环境均为68407465，无重复启动或自动回滚。备份审计已以真实运行状态协调为started，并保留初始错误及同PID复核记录。

部署工具修复单独提交`c1985ff59d0c36cad402278b9c37e3b951a46de5`：最多观察10秒，实际启动条件满足即返回，持续错误或服务退出仍报错；26项测试通过。该提交已从GitHub只读checkout，用于后续部署/回滚工具。它只改部署脚本和测试，没有为此重新启动应用，应用仍是68407465。

## 备份与回滚

私有备份：`/data/pipeline-dispatch/maintenance/gpu-full-offload-20261105T0232Z-68407465`，含原unit、30/99配置、私有manifest/env及运行来源证据。回滚目标是179596a加f3bd385五文件租约补丁；不是裸179596a。凭据、任务账本、代际、回执和9条历史unknown发布均原样保留。

回滚仅在用户已明确暂停本节点、在途任务安全结束后执行；不暂停其他节点或全局渠道，不清空未知结果。先安全停止本节点并确认inactive，再运行已签入GitHub的工具：

```sh
systemctl stop pipeline-node-hk-gpu.service
/opt/pipeline-dispatch/runtime/python/bin/python3.10 \
  /opt/pipeline-dispatch/releases/c1985ff59d0c36cad402278b9c37e3b951a46de5/scripts/switch_gpu_overlay_release.py rollback \
  --current /root/pipeline-node-gpu.json \
  --backup /data/pipeline-dispatch/maintenance/gpu-full-offload-20261105T0232Z-68407465 \
  --user-paused-confirmed
```

该回滚恢复原unit/manifest/exact99配置后启动原节点，不恢复数据库。不使用force kill。若节点后来版本或配置已变，先核对实际差异，不能盲目套用旧备份。
