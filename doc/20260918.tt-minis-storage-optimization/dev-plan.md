# 实施计划

1. 独立 worktree 从与线上内容一致的 fb4c8ca 建分支。
2. 快照复用、登记、租约、pin、锁和受保护回收。
3. 发布分区复用、回填失效、manifest 引用清理及计时日志。
4. 测试和文档；精确提交推送 GitHub。
5. CPU 数据盘拉取精确提交，先运行 Linux 测试，再在既有报表锁下备份和切换。
6. 生产缓存首次建立分区索引，核对旧/新分区业务内容；第二次验证全部复用。
7. 复核冻结快照清单，仅退休即时验证通过的文件，记录回执。

本地验证：python -m compileall -q ops/tt-minis-native-growth；目录内 python -m unittest discover -p 'test_*.py' -q；git diff --check。
