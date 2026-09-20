# 验证报告

## 本地实现验证

- Drama：361 tests OK，6 项既有 COS SDK 环境测试跳过；新增 20 项全部通过。
- TT：125 tests OK；Intel OpenCL 旧新 shader 实测通过，旧输出字节相同；3 组新参数每组 90 个通道误差小于 1/255。
- FB：37 tests OK；真实 FFmpeg 旧/新 × 有声/无声共 4 次渲染，单音轨且音频 PCM 一致。
- 集成独立复核：110 tests OK；实际 Drama legacy adapter 再做 4 次真实 FFmpeg 渲染通过。
- 后台两个 inline JavaScript 块 node --check 通过；Python 编译和 diff whitespace 检查通过。

## 生产验证

待独立 release 真实 GPU 预检、8 个输出完整解码、三路上线和 CPU 读回后补充。
