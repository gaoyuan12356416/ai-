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
- TT 竖版 60 秒、横版 60 秒、分辨率变化 60 秒、延迟 61 秒的四组源帧时间轴比较，mismatch 均为 0。
- CUDA、OpenCL 预检各验证 80 个不可变 RGBA 缓存。最终 Drama 提交仅改变旧 FB 适配器的音频结束参数；逐文件核对原生模块、内核、worker、预检代码与先前已通过版本完全一致后复用该缓存预检证据，同时验证最终 release 身份和导入。未声称最终提交重新执行完整缓存扫描。
- 独立验收没有上传或发布业务素材。报告：HK `/data/drama-synthesis-gpu/work/source-overlay-qa-20260920/report.json`；本地 `output/random-source-overlay-release-20260920/gpu-acceptance.json`。
- 60 秒验收视频 SHA256：`9d7a07c45ee4ee984128c9077a992fff533dc564aa4fb5fe8681b53662eaa469`，本地复制后散列一致并抽帧检查。

## 生产切换

生产旧任务自然排空后再切换；最终指针、进程、CPU/UI 和入口恢复结果在此追加。
