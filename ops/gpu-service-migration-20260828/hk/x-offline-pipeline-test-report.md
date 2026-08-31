# X完整离线处理验收记录

日期：2026-08-28。批次：gpu-service-migration-20260828T1502。

## 结论与范围

第三轮对精确170e3现行源码完成完整离线流程PASS：真实NVENC处理、输出校验、FakeCOS验证、独立manifest及第二次reuse全部通过。

前两轮失败状态保持：首轮是验收器额外加入CUDA解码断言导致失败；修正后的第二轮正确拒绝已变化的生产版本。旧两轮unit和证据均保留，不覆盖历史结果，不自动重试。第三轮使用新提交、新目录、新unit，并另获单次执行授权。

上述两轮验收以fba8为冻结基线，调用真实MediaRepairProcessor、私有FFmpeg/FFprobe和NVENC编码，输入是本地2秒合成视频，下载/COS/HTTP均为隔离替身。生产state只读，生产config不可访问，独立网络命名空间，临时文件仅进入本批次数据目录。未使用生产凭据、真实COS或平台API，未创建生产修复记录或帖子。

此前x-offline-ffmpeg.json和x-offline-nvdec-nvenc.json仅为直接FFmpeg烟测，不能替代整条业务处理流程的验收。

## 前两轮执行摘要

| 项目 | 首轮 | 第二轮 |
| --- | --- | --- |
| 已推送脚本SHA | c10fd5c979522a190a9e7a4a31dd6cc55bd5b9d1 | 56517311cac2c253b7ce8ebb2f294fadcfa2da2c |
| 独立unit | gpu-migration-x-offline-c10fd5c97952.service | gpu-migration-x-offline-56517311cac2.service |
| 启动次数 | 1 | 1 |
| ExecMainPID | 1776864 | 1781899 |
| Result / ExecMainStatus | exit-code / 1 | exit-code / 1 |
| 媒体子命令 | 4，全部返回0 | 0 |
| 失败位置 | 额外CUDA解码断言 | 现行current不再是冻结fba8版本 |
| 总体结果 | 失败，不改为PASS | 守卫拒绝，未执行媒体处理 |

两轮均为静态oneshot，无Install/timer/Restart，不enable，超时上限90秒，PrivateNetwork=yes、ProtectSystem=strict，读写范围仅各自验收目录。第二轮启动前显存空闲超过14GiB，systemd-analyze verify退出0。

## 首轮保留的真实媒体证据

证据目录：

    /data/migrations/gpu-service-migration-20260828T1502/x-offline-pipeline/c10fd5c979522a190a9e7a4a31dd6cc55bd5b9d1

实际执行了合成输入、输入FFprobe、冻结业务NVENC编码、输出FFprobe，四个命令全部返回0。冻结业务采用默认解码及CPU滤镜，编码器为h264_nvenc，本来不要求-hwaccel cuda。错误来自验收器后加的CUDA解码断言，不是这四个媒体命令失败。

独立manifest为ready，profile为x-h264-nvenc-720-duration-policy-v5。保留的输出元数据为H264 High、yuv420p、逐行1280×720、30fps、AAC-LC 48000Hz双声道、2.005秒，文件1689374字节。

输出SHA256：

    070f121667cad779cd969910819b117880e8195d7e981e88cca5f8ed5f801ab0

独立manifest job key：

    a8a031a5c98e40854808233140d6622b3f7ef0ce98dfad523a89d39639b06c66

产物保存在该证据目录的fake-cos子树，manifest位于processor-state/manifests。首版尚无processor-results.json独立中间结果文件；不把有产物或四条命令成功写成整体验收成功。

修正版仅删除验收器额外的CUDA解码要求，仍要求恰好一个成功的原业务h264_nvenc命令，并新增默认解码回归和processor-results.json。冻结业务源码未修改。

## 第二轮版本守卫拒绝

第二轮证据目录：

    /data/migrations/gpu-service-migration-20260828T1502/x-offline-pipeline/56517311cac2c253b7ce8ebb2f294fadcfa2da2c

保留unit-preparation.json、operator-start.json、attempt.json、result.json、operator-completion.json和production-drift-readback.json。单次start于17:38:33结束，耗时0.187秒；result为ok=false、ValueError、offline_acceptance_failed、commands=[]。

此次未进入MediaRepairProcessor，因此没有新的媒体SHA、FFprobe结果、processor-results或reuse次数。不得用首轮产物填充第二轮缺失的验收证据。

只读核实发现：

- current已指向/data/x-post-media-repair/releases/170e3b1325b71a72fcd6de913982ce92bb77fa40。
- 生产X PID1780296于17:34:38 CST启动，早于本轮离线unit；其cwd同上述release。
- 协调者确认，变更来自用户另一项获授权任务“解释发布前检查失败原因”的X下载完整性修复升级。本迁移不覆盖或回滚该版本。
- 旧fba8冻结文件仍保留且哈希匹配。新版本的worker入口和media_repair.py哈希与旧版相同，但service.py已变化；不能擅自把整个新版本视为冻结基线。
- 现行unit SHA与本迁移staged-unit完全一致，DropInPaths为空；版本变化来自current切换，不能只靠unit SHA判断版本未变。

生产unit SHA256：

    2a136c1358b1261a98f115424895e2a8ca225cc3f91939f0aabbfe6d6ebbdceb

第二轮验收前后不变量：

| 生产服务 | 验收前后PID | 验收前后NRestarts |
| --- | --- | --- |
| x-post-media-repair.service | 1780296 | 0 |
| fb-page-random-overlay-gpu.service | 1207342 | 0 |
| drama-synthesis-gpu-worker.service | 1188891 | 0 |

三项服务保持active，170份生产manifest哈希全部不变。生产X的/tmp和/var/tmp分别与/data/x-post-media-repair/state/tmp、state/var-tmp的设备及inode匹配。首轮unit-preparation、operator-start、attempt、result、operator-completion五份关键证据SHA前后全部不变。

未打印journal正文或凭据。未停止、重启、重新配置生产X/FB/剧集，未改生产current或打开生产闸门。

## 第三轮验收基线

第二轮结束后停止了额外测试。随后协调者与另一个任务确认170e3b1325b71a72fcd6de913982ce92bb77fa40为现行版本，先仅授予本地验收来源更新权限；完成统一push部署后，才另外授予第三次单次启动权限。

本地Git对象确认该精确commit可从refs/remotes/origin/codex/x-pool-blockers-20260828追溯。逐项读取该commit的blob，计算SHA256，与第二轮香港production-drift-readback.json的实际文件哈希一致。只冻结精确170e3b，不使用远程分支当前tip或其他版本替代。

| 验收源码 | Git blob ID | SHA256 |
| --- | --- | --- |
| features/x_posts/media_repair.py | 58fab535bf6dbefff8117f5742c5c3f410ac4a81 | 09dfeba82598a3cce0dd483cb5b091434deb9f5f814d37099b118d0666310f3c |
| features/x_posts/service.py | 43478231e5e9ce5ee4a5d3d492bc6b8700b6115e | e63a4e04b622b95b0a61489e90984b03317183726ac8420ceb9f5e0e427356e5 |
| features/x_posts/__init__.py | 6e2ed4bac31cf0d86c08d8edbe075cc7f207b9e6 | b8c8436310bd08e710f62b0d6ce0623b6c282a562a5fe0112f4f69dd10cefbc3 |
| features/__init__.py | e69de29bb2d1d6434b8b29ae775ad8c2e48c5391 | e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855 |

附带核对worker入口Git blob为82b392fadd82ae09b0ee0c0f5c093dc6fd609bb8，SHA256为41e9c45d11535b79c2f06757d33778e887156319cb62756fd02e6e3ed2c52f56，与香港证据及旧入口相同。验收不会启动该HTTP worker，只导入冻结的处理器。

仅x_offline_pipeline.py的精确release常量及已变化的service.py哈希更新，结果新增source_release_sha/source_root以区分生产源码版本与验收工具版本。current、源码哈希、导入路径守卫全部保留；不修改deploy.py的迁移旧基线、systemd模板或任何生产文件。新增回归覆盖正确冻结源码允许，以及current变化、源码篡改、导入来源变化必须拒绝。

## 第三轮实际执行：PASS

验收工具SHA：82788616355f019a1686f6f10c3ac618756e2ab1。

生产源码SHA：170e3b1325b71a72fcd6de913982ce92bb77fa40。

独立静态unit：gpu-migration-x-offline-82788616355f.service。经systemd-analyze verify成功后只start一次，2026-08-28 17:59:40 CST完成，等待start返回耗时1.888秒。start返回0、Result=success、ExecMainStatus=0、result.ok=true。成功oneshot退出后状态为inactive/dead，systemd读回ExecMainPID=0，不将此解释为失败或伪造执行PID。

证据目录：

    /data/migrations/gpu-service-migration-20260828T1502/x-offline-pipeline/82788616355f019a1686f6f10c3ac618756e2ab1

保留unit-preparation.json、operator-start.json、attempt.json、result.json、processor-results.json、operator-completion.json、retained-media-readback.json，以及合成输入、FakeCOS实际成片和独立manifest。

执行前MemAvailable超过30GiB、GPUfree14916MiB，无正在运行的非X媒体进程。unit实际配置为PrivateNetwork=yes、ProtectSystem=strict、MemoryMax=2147483648、CPUQuotaPerSecUSec=2s、TasksMax=128、TimeoutStartUSec=90秒。生产state/releases/runtime只读、生产config不可访问，唯一可写业务区域为本轮验收目录；脚本验证了独立网络及临时目录绑定。

| 验收项 | 实际结果 |
| --- | --- |
| 首次真实processor.repair | ready，reused=false |
| 第二次相同请求 | ready，reused=true |
| Fake下载 / FakeCOS上传 / HEAD | 1 / 1 / 3 |
| 复用前后媒体命令数 | 4 / 4，四条命令均退出0 |
| GPU编码 | 原业务h264_nvenc，未额外要求CUDA解码 |
| 保留成片 | 1689374字节，2.005秒 |
| 视频 | H264 High，1280×720，30fps，逐行，yuv420p |
| 音频 | AAC-LC，48000Hz，双声道 |
| 真实COS / 平台调用 | 0 / 0 |

输出SHA256：

    070f121667cad779cd969910819b117880e8195d7e981e88cca5f8ed5f801ab0

独立manifest job key：

    c462e47dcb765bf3f2efe434241cc62de0871faf6b28ad73fbc6e0a7b81a9c3c

复用前后manifest SHA256相同：

    13a0e3422d82dd2a724cf5f9a2bafd7afaf90ebf80bb9375e05a0cda37e3cabc

结果文件SHA256：

- result.json：b5917246666322fcf3a5158f2e436feb7822d9ecf7560c5d6e660800f6ab5433
- processor-results.json：0b58871df4d39e60317dc99f6f3d2b552bc844bd9d2cb88ac05bff3bf27f57d9

验收结束后只读复核保留成片的完整SHA、文件大小及manifest哈希；额外FFprobe仅允许file/pipe协议、无凭据环境，退出0并确认同样的视频/音频元数据。首次与复用返回值除reused外完全一致。

生产X1780296、FB1207342、剧集1188891的PID/NRestarts及unit SHA前后不变，全部保持active。X current保持170e3，运行路径与/tmp、/var/tmp绑定不变；测试job未进入生产manifest。本次170份生产manifest哈希也恰好全部未变。正常生产若自然新增manifest，只记录差异，不能要求全局零变化或因此回滚；隔离与测试job缺席才是本验收的写入边界。

c10/565两轮既有JSON证据及旧unit哈希均保持。未重启旧验收unit，未改动生产service、tunnel、current或CPU。该PASS仅覆盖本地合成输入和Fake上下游的真实处理流程，不代表真实下载、COS上传、X平台发布或新下载完整性修复的全部边界测试已被此单个用例覆盖。

## 本地验证

本地既有测试命令：

    python -m unittest discover -s ops/gpu-service-migration-20260828/hk/tests -v

精确170e3b基线的23项测试全部通过，包括默认解码+h264_nvenc允许、缺失/失败/重复NVENC及仅libx264拒绝，以及新增的版本/哈希/导入守卫回归。本地Git blob SHA核对4/4通过，Python3.9语法检查及git diff --check通过；deploy.py与systemd模板无差异。第三轮执行后的补充仅为证据记录，不再修改业务或验收代码。
