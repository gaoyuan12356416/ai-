# 测试报告

本地与生产验收通过，2026-09-18 18:22（北京时间）正式生效。

- Drama相关113项测试通过：目录版本、CPU目录、GPU runtime及合成相关合计113项（命令：python -m unittest scripts.test_drama_catalog_versions scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_gpu_compositor_v2 -v）。
- TT独立基线提交326e16defb8f0b1aec76e8f36d52bb6359275a98：新增17项和既有102项，共119项通过。全部发布路径测试为假API。
- 20个媒体全部通过：10个PNG与批准图层逐像素一致；10个WebM尺寸720x1280、120帧、30fps、4秒、无音轨、透明度最大舍入1/255。合成像素平均误差最大0.089/255。循环端点一致，纸签为刻意的小幅摆动，共7个不同光栅状态。
- 原20条manifest记录不变；1000个确定性配方覆盖全部38个有效素材；light仍保留2个但不参与。
- py_compile、git diff --check通过。

生产验收：

- 两套缓存各38项全覆盖，旧素材与manifest SHA保留；实际服务UID读取通过。
- Drama实际运行时预检：ok=true、issues=[]、cache=38、CUDA=true、完整app import=true。
- 5个手动四层组合覆盖全部20个新增素材，每段5秒，包含跨4秒循环；旧SHA配方在新默认下实际渲染成功。
- TT HEVC及FB H264分别实际制作5秒720x1280样片；未调用上传或真实发布。
- HK三个worker及隧道、CPU API/job worker全部active、NRestarts=0；各健康200，真实目录8/10/8/12，公共topbar200。
- TT七个触发器原状态恢复、FB继续领取进程精确身份恢复、prepare.timer active、维护gate为空。
- 发布结论：通过。历史完成结果与冻结配方保留；未来制作采用扩展默认目录。

旧Git镜像缺失对象通过独立GitHub浅克隆规避；原生依赖通过真实进程PYTHONPATH加载。
