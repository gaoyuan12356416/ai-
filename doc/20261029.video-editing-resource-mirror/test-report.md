# 测试报告

最终本地集成：`python -m unittest discover -s tests -p 'test_video_editing*.py'`运行207项，206通过、1可选COS SDK跳过，27.761秒。覆盖资源目录、上传续传、草稿、单元局部错误、幂等、冻结归属、入库、v1/v2配方、旧/紧凑导出profile和部署恢复。`node --test tests/test_video_editing_frontend.js tests/test_video_editing_browser_contract.js`运行61项，全部通过。

真实FFmpeg验证底部/顶部字幕保护、整帧镜像、淡化拼接、音画时长、横竖画面、SAR及旋转；40组无镜像命令与线上紧凑导出逐字一致。8组Canvas/FFmpeg几何一致。

真实Chrome验证四栏目、资源先选、同资源素材选择、字幕保护开关、撤销、播放暂停后拖动保护边界960→804、v2草稿保存。Canvas像素断言确认保护开关与撤销实际更新画面；无JS错误。证据存于本次聊天work/frontend-work/与work/mirror-evidence/，不纳入业务素材。

最后两项发布前补充回归通过：明确版本冲突解除前端待核实状态；GPU恢复正常worker时补启隧道，部署测试14项通过（包含于207项总数）。

香港GPU真实NVENC验证通过：现有Python3.10.20、FFmpeg7.1，旧画面、镜像默认字幕带、无保护、自定义顶部保护、交叉淡化全部生成有效720×1280/30fps/H.264/AAC成片。字幕带与原图平均像素误差0.013，错误镜像对照误差98.92；保护区外镜像平均误差0.027。证据：/data/drama-synthesis-gpu/validation/resource-mirror-6f9190e/mirror-canary-nvenc/result.json。该验证只生成隔离本地媒体，不调用业务API、数据库或COS。

生产浏览器验收：四栏目加载；21481精确搜索返回Love & Revenge of Malos；资源筛选视频入口含免费集/素材库/在线剧集；在线目录返回其产品和语言版本。新增“升级验收 · 未完成草稿 20261029”，空片段仍保存到v2；制作只显示该单元缺片段，任务数仍为7。旧“一期验收 · 两剧独立剪辑”保持v16、两单元原5+2片段，可播放，未保存或改写旧草稿。附加本地浏览器旧v1升级视图验证PATCH次数为0。

生产状态：CPU API/剪辑worker/入库worker、GPU worker/tunnel均active；入口恢复；14份CPU目标文件哈希匹配；两端历史task/output/material账本不变；GPU既有SAFE_HEALTH配置不变；无关CPU服务PID未变化。上线未执行真实业务成片入库写入，相关冻结归属和幂等行为由接口/集成回归覆盖。
