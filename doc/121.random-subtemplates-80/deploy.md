# 部署与回滚

状态：准备中，尚未切换。

目标：HK43.154.250.89三个随机制作worker，CPU43.166.187.96 API/job-worker目录配置。线上代码保持Drama9c4c3cb、TT326e16d和FB e3bef98（以切换前再次核对为准）。

当前目录24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c；原始028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f同样保留。新目录 `b5df776a88bdfa961e60f60d6076c6915f37c5fd5ed8ade973dc4be7205eea6c`。对应HK共享根 `/data/random-overlay-gpu/assets/catalog-b5df776a88bd`，Drama隔离根 `/data/drama-synthesis-gpu/assets/catalog-b5df776a88bd`，CPU元数据 `/mnt/data-disk/drama-synthesis-catalog/catalog-b5df776a88bd/manifest.json`。

备份目标：HK /data/random-overlay-gpu/backups/20260920-subtemplates80；CPU /mnt/data-disk/random-overlay-gpu/backups/20260920-subtemplates80。原始env仅保存在权限0700的服务器备份内。

上线前GitHub提交/拉取、旧文件验证、缓存和私有样片必须通过。保存TT/FB原trigger状态，暂停新领取，等待既有制作和HTTP请求排空；三个GPU使用追加EnvironmentFile切新默认，CPU最后只改两项目录配置。worker和隧道分别确认就绪后恢复原trigger。

回滚：以同样维护/排空流程将三GPU默认ROOT/SHA及CPU MANIFEST_FILE/SHA回退9月18日24d6fa目录。保留新配置中的三个SHA映射、全部素材和缓存、线上兼容代码。窄重启三个GPU及三个隧道，再重启CPU API和job worker；确认健康后恢复原trigger。禁止删除新目录、恢复旧DB或重制已完成任务。
