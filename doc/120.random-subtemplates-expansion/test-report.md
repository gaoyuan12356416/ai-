# 测试报告

本地验收通过，生产验收待执行。

- Drama相关113项测试通过：目录版本3、CPU目录16、GPU runtime及合成相关94（命令：python -m unittest scripts.test_drama_catalog_versions scripts.test_drama_synthesis_cpu_catalog scripts.test_drama_synthesis_gpu_runtime scripts.test_drama_gpu_compositor_v2 -v）。
- TT独立基线提交326e16defb8f0b1aec76e8f36d52bb6359275a98：新增17项和既有102项，共119项通过。全部发布路径测试为假API。
- 20个媒体全部通过：10个PNG与批准图层逐像素一致；10个WebM尺寸720x1280、120帧、30fps、4秒、无音轨、透明度最大舍入1/255。合成像素平均误差最大0.089/255。循环端点一致，纸签为刻意的小幅摆动，共7个不同光栅状态。
- 原20条manifest记录不变；1000个确定性配方覆盖全部38个有效素材；light仍保留2个但不参与。
- py_compile、git diff --check通过。

生产须完成两套缓存各38项、三制作入口新旧目录契约和健康检查后才宣布生效。
