# 实施计划

新增channels.py、cache.py，接入独立runtime/source/service；仅修改主app的YouTube路由方法。前端并行auth/bootstrap，频道状态后台刷新，新建预取素材，保留既有DOM同步。以精确旧handler SHA替换生产handler，保护其他功能最近改动。

校验：python -m py_compile；python scripts/test_youtube_channel_cache.py -q；python -m unittest discover -s scripts -p test_youtube_auto_*.py -q；node --check static/youtube-publish.js；既有图像/失败通知回归；浏览器频道缓存与6秒闪屏QA；server Python3.9复验。GitHub提交后精确归档部署，先备份且任务空闲才重启API及依赖worker。
