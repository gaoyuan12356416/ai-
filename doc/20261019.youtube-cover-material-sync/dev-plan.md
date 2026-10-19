# 开发计划
1. 新增独立 ops/youtube-cover-material-sync，同步源快照、出站账本、COS镜像、既有ai-source、SQLGate读回。
2. 单测异常与重复、存量/增量边界；python -m py_compile sync.py deploy.py；python -m unittest -v test_sync.py。
3. GitHub推送后CPU检出精确SHA，备份独立服务配置并只安装新服务。
4. 服务器只读plan并冻结清单；先小批验证，再启用持续worker，导出逐素材回执。
