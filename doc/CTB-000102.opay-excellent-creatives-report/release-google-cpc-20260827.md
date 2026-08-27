# Google CPC / 图片视频 CTR 发布记录

2026-08-27 北京时间15:22已上线：https://ai.yingliangads.com/reports/opay-excellent-creatives/ 。本记录是实际执行结果，取代旧交接段落中的PENDING；不修改历史V1/V2记录。

## 实际版本与结果

- GitHub分支：`codex/opay-google-spend-top50-20260827`。
- 运行提交：`72f2e7440e16d8d3ea782ce9eea31176d21c0797`；其生成器、Google模块、HTML与回填提交`7cea6648fe58e9f24c6c1c2fd00ddc91e7242bed`逐字节相同，后一个提交只补验收器/测试/评审记录。
- 主机：`43.166.187.96` / `VM-0-108-centos`。
- current：`/opt/opay-excellent-creatives/releases/72f2e7440e16d8d3ea782ce9eea31176d21c0797`。
- 新缓存：`/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-google-cpc.sqlite3`，从旧V2在线一致性克隆，未原地修改旧缓存。
- 正式数据版本：`20260827T152235588279+0800`；latest SHA256：`465e4e10c9c1cf9c38ecf24a246a47bdf84fe2d782022fc03b98e11ef6693b13`。
- 影子版本：`20260827T151918612513+0800`；正式7个月JSON除`data_version`外与已验影子逐字段一致。发布没有refresh/rebuild。
- 公开静态目录：`/usr/share/nginx/html/reports/opay-excellent-creatives`。

| 月份 | GG优秀素材 | GG视频/图片 | 保留Meta/TT | 全渠道总行数 |
| --- | ---: | --- | ---: | ---: |
| 2026-01 | 0 | 0 / 0 | 34 | 34 |
| 2026-02 | 0 | 0 / 0 | 26 | 26 |
| 2026-03 | 0 | 0 / 0 | 18 | 18 |
| 2026-04 | 4 | 3 / 1 | 23 | 27 |
| 2026-05 | 5 | 3 / 2 | 32 | 37 |
| 2026-06 | 4 | 3 / 1 | 27 | 31 |
| 2026-07 | 6 | 4 / 2 | 26 | 32 |
| 合计 | 19 | 13 / 6 | 186 | 205 |

GG本批19行均为NG OPay、B组。A逻辑已实现，但这些历史scope因平台USD不完整、完整映射消耗不足平台50%、或平台无消耗/点击而暂停，不用已映射素材池替换全平台分母。1—3月无历史FX明细，不以当前汇率补造NGN金额。

7月NG图片视频基准=`7,450,399 / 319,809,411 = 2.3296371976%`。6行合计USD54,872.90，真实custom_source.id：2072578、1508604、2786191、1337250、3368139、3393516。NG平台仍有10条Campaign日记录缺可核验历史汇率，A及平台USD/CPC留空；PK图片视频CTR4.0192786847%，映射覆盖不足50%，无B入选。

## 验证证据

证据根：`/mnt/data-disk/opay-excellent-creatives/qa/acceptance-20260827-google-cpc`。

- 本地及最终服务器提交：148后端测试通过；前端46行为用例通过。独立评审选优/映射均通过；BUG-GCP-001/002已修复。
- `seven-month-reconciliation.json`、`production-seven-month-reconciliation.json`：全部七个月PASS；从原始SQLite逐素材重算A/B、排除集合、CTR/CPC、排名、完整性、FX/映射缺口。Meta/TT所有行/基准/审计字段（含metrics）完全一致，旧`platform_daily/af_daily/material_daily/daily_audit/ads_source_dim`逐行哈希一致。
- `july-live-source-audit.json`：63350、`@@read_only=1`，手工直读1连接；独立读取140,003条事实、6,391个资产全部映射候选、816条FX明细，精确映射集合/原币/USD/最终6行与新缓存一致。不复用生产映射、汇率或选优函数。
- 7个月真实JSON执行实际HTML的CSV生成/Blob路径，全部与Google筛选共14个CSV逐字段PASS，位于`csv/`；7月26条非GG冻结回归PASS。完整205行源文件/缩略图状态均available，链接均HTTPS。
- `rollback-drill-result.json`：独立影子注入latest提交失败，旧manifest逐字节不变；成功后恢复旧manifest/HTML/release，旧新月份文件均保留。没有为演练切回生产。
- `promotion-operation.json`、`promotion-result.json`：持有独立锁，暂时停止本报表两个timer，原子切current和latest；HTTP首页/latest/7个月JSON全部200且noindex，无鉴权跳转；HTML/latest no-store，月JSON immutable。
- 实际公开浏览器显示正式版本、7月Google6行和54,872.90；VID筛选4行/33,172.02；素材1508604详情CPC0.054844、基准CTR2.33%、A暂停原因正确。该视频加载1280×720、29.566667秒、readyState4、无media error。
- 实际390×844移动端无页面横向溢出，宽表client349/scroll2260，可横滚（scrollLeft实测165）。恢复默认视口。
- 原生浏览器下载事件等待12秒未捕获；不宣称已确认用户下载目录或落盘成功。真实CSV内容/Blob验证通过，不能将浏览器回执未捕获等同于内容核验失败。
- 配置env/Nginx/service/timer哈希不变；nginx -t通过；未reload、未daemon-reload、未重启主服务。主站200、AI Game Performance302与发布前一致。
- 两timer恢复enabled/active，下次2026-09-03/05北京时间10:00（保留原随机延迟）。回填oneshot正常退出0。

## 当前回滚点与精确程序

备份：`/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-cpc`；备份manifest SHA256=`4e97d77b1c301d249de9558963bb0bb9ca63538ed600c48d673a54cf2cdaff17`。备份含一致性旧V2缓存、7个月公开数据、HTML/latest、env/Nginx/unit/timer。旧缓存`cache/opay-excellent-creatives-v2.sqlite3`及其release均原样保留。

回滚目标为旧V2提交`533bbac77ae29d437d084732b7fddfc022754a93`，旧data_version=`20260827T120935529360+0800`，旧latest SHA256=`f5ae1d646c8522758d23d158dec1e545aa5dc26914581dd5a18c05a493b6cecb`。不是旧V1首次部署的下线路由命令。

以下生产回滚命令未执行；相同顺序已在影子演练。后续版本/配置若漂移，断言会停止，不盲目覆盖。失败后保持timer暂停直到代码与数据一致。

```bash
flock -n -E 75 /tmp/opay-excellent-creatives.lock python3 - <<'PY'
import pathlib,json,hashlib,os,uuid,tempfile,subprocess,sqlite3,contextlib
b=pathlib.Path('/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-cpc')
w=pathlib.Path('/usr/share/nginx/html/reports/opay-excellent-creatives')
c=pathlib.Path('/opt/opay-excellent-creatives/current')
assert c.resolve().name=='72f2e7440e16d8d3ea782ce9eea31176d21c0797'
assert json.loads((w/'latest.json').read_text())['data_version']=='20260827T152235588279+0800'
assert hashlib.sha256((b/'manifest.json').read_bytes()).hexdigest()=='4e97d77b1c301d249de9558963bb0bb9ca63538ed600c48d673a54cf2cdaff17'
m=json.loads((b/'manifest.json').read_text())
for path,info in m['files'].items():
    assert hashlib.sha256(pathlib.Path(info['backup']).read_bytes()).hexdigest()==info['sha256']
    if not path.startswith(str(w)):
        assert hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()==info['sha256']
old=json.loads((b/'latest.json').read_text())
for entry in old['months']:
    assert (w/'data'/old['data_version']/(entry['month']+'.json')).is_file()
with contextlib.closing(sqlite3.connect(pathlib.Path(m['cache']).as_uri()+'?mode=ro',uri=True)) as db:
    assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
for stage in ('initial','final'):
    assert subprocess.check_output(['systemctl','show','opay-excellent-creatives-refresh@'+stage+'.service','--property=ActiveState','--value'],text=True).strip()=='inactive'
timers=['opay-excellent-creatives-initial.timer','opay-excellent-creatives-final.timer']
subprocess.run(['systemctl','stop']+timers,check=True)
for name in ('latest.json','index.html'):
    fd,tmp=tempfile.mkstemp(prefix='.'+name+'.rollback-',dir=str(w))
    with os.fdopen(fd,'wb') as stream:
        os.fchmod(stream.fileno(),0o644);stream.write((b/name).read_bytes());stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,w/name)
link=c.parent/('.current-rollback-'+uuid.uuid4().hex)
os.symlink(m['current'],link);os.replace(link,c)
assert str(c.resolve())==m['current']
assert (w/'latest.json').read_bytes()==(b/'latest.json').read_bytes()
subprocess.run(['systemctl','start']+timers,check=True)
print('RESTORED',old['data_version'],m['current'])
PY
curl -fsS https://ai.yingliangads.com/reports/opay-excellent-creatives/latest.json
systemctl list-timers 'opay-excellent-creatives*' --all --no-pager
```

不删除新旧release、缓存、月份版本或媒体；旧release默认回到旧V2缓存。由于配置未改，无需Nginx或主服务重启。文档提交可晚于运行提交，不要求为更新本记录再切一次运行版本。
