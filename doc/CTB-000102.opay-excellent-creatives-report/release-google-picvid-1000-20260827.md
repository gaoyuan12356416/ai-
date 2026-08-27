# GG 图片/视频基准与1000美元门槛发布

状态：已上线。2026-08-27北京时间17:33完成七个月原子发布；随后发布仅修正AF缺失说明的前端补丁。最终线上运行提交为 `295b14d162b85a397e02dfcdbd637ef4a497e4b5`，全部数据与用户确认的7月46条审批样本一致。

## 已批准范围

2026-01—2026-07仅GG刷新；A分母/CPC、B的CTR均使用全部图片视频资产（含未映射），按NG/PK分别加权汇总。A不设最低消耗；B严格>1000美元。7月批准样本：NG40、PK6；A-only8、B-only28、A+B10；USD116640.81。

## 部署及回滚设计

- 旧current：`/opt/opay-excellent-creatives/releases/72f2e7440e16d8d3ea782ce9eea31176d21c0797`。
- 旧data_version：`20260827T152235588279+0800`；latest SHA256：`465e4e10c9c1cf9c38ecf24a246a47bdf84fe2d782022fc03b98e11ef6693b13`。
- 数据盘UUID已核对：`3e8ac4e8-7770-456d-9e89-2ec5dd405fa8`；2026-08-27预检可用77G，根7.1G。所有持久报表数据放数据盘。
- 新缓存：`/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-google-picvid-1000.sqlite3`。
- 备份目标：`/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-picvid-1000`。
- 影子目录：`/mnt/data-disk/opay-excellent-creatives/staging-public-google-picvid-1000`。
- 验收目录：`/mnt/data-disk/opay-excellent-creatives/qa/acceptance-20260827-google-picvid-1000`。
- 先GitHub精确commit，再在服务器独立release运行；`--clone-cache-from`克隆旧库，`--backfill --from-month 2026-01 --to-month 2026-07 --google-only --refresh --rebuild`重算。正式切换前必须运行独立raw-cache校验器，包含`--approved-july fixtures/2026-07-google-picvid-approved.json`。
- 切换前保存HTML/latest、配置哈希及旧库一致性备份；发布持有独立锁，确认refresh服务无在途，暂停本报表timer；发布失败时恢复旧HTML/latest/current后恢复原timer状态。不修改Nginx/env/unit或其他报表，不重启主服务。
- 手工回滚：在独立锁内确认当前为本release与记录的数据版本；暂停两个timer，恢复备份HTML/latest，原子切current至旧72f2e7，验证旧manifest哈希后恢复timer。旧release默认指向旧google-cpc缓存，保留新旧数据，不覆盖旧缓存。

## 实际执行记录

### 版本及部署边界

- 主机：`43.166.187.96`，`VM-0-108-centos`。
- GitHub分支：`codex/opay-google-picvid-1000-20260827`；最终运行提交 `295b14d162b85a397e02dfcdbd637ef4a497e4b5` 已推送、服务器fetch并核对精确SHA。
- 运行目录：`/opt/opay-excellent-creatives/releases/295b14d162b85a397e02dfcdbd637ef4a497e4b5`；`current` 已原子指向此目录。
- 公开目录：`/usr/share/nginx/html/reports/opay-excellent-creatives`。
- 数据版本：`20260827T173321821817+0800`；latest SHA256：`fbb6e5990d1dbab09273ad6ce0834f679603466c5943814ea26cac335dcda95c`。
- 最终HTML SHA256：`803991e83d8c1aeaa6e9997ca7fa7d78b5888da056aff25b60a15197fb05d472`。
- 持久缓存：`/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-google-picvid-1000.sqlite3`，从冻结旧库一致性克隆，再只读补GG事实；未修改旧库和Meta/TT基础事实。
- 仅短暂停止本报表两个timer以避免切换竞态，切换后均恢复enabled/active。未修改env、Nginx或systemd unit；未重启/reload主站服务。下次初版2026-09-03 10:00:17 CST、终版2026-09-05 10:00:23 CST，保留原3日/5日及随机延迟。
- `nginx -t`通过；主站和报表HTTP200，报表无登录跳转。Nginx/env/unit文件哈希与发布前一致。
- 本次由本代理实现、自审及执行验收；独立校验指不依赖生产选优函数的计算器，不冒充独立人员签字。未更新个人技能或长期记忆。

### 刷新结果

| 月份 | GG NG | GG PK | GG合计 | Meta/TT（未变） | 报表合计 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 2026-01 | 0 | 0 | 0 | 34 | 34 |
| 2026-02 | 0 | 0 | 0 | 26 | 26 |
| 2026-03 | 0 | 0 | 0 | 18 | 18 |
| 2026-04 | 28 | 0 | 28 | 23 | 51 |
| 2026-05 | 29 | 8 | 37 | 32 | 69 |
| 2026-06 | 28 | 9 | 37 | 27 | 64 |
| 2026-07 | 40 | 6 | 46 | 26 | 72 |
| 合计（月度行，非跨月去重） | 125 | 23 | 148 | 186 | 334 |

7月仅A 8条、仅B 28条、A+B 10条；NG40条USD116630.55，PK6条USD10.26，共USD116640.81。A没有最低消耗，PK六条USD1.48—2.04按已批准定义保留。

7月NG图片视频基准：USD358682.21、7450399点击、319809411曝光，CPC约0.04814268美元、CTR约2.329637%；PK基准USD121.72、4178点击、103949曝光，CPC约0.02913356美元、CTR约4.019279%。基准包含未映射资产，不以入选或已映射素材为分母。

1—3月GG无入选，NG历史汇率缺口仍披露，不使用当前汇率补金额；PK无消耗/不满足门槛的月份如实为0。4月PK完整精确映射消耗覆盖不足50%，A暂停且没有B入选。GG素材安装、AF、CPA/APM等保持null，平台图片视频池没有同范围AF也保持null。

### 执行链及缺陷闭环

1. `3c9c0a02f2bbc5b8b387291a324dc3ea5b27e576` 完成新口径和只读七个月源事实刷新。一次性单元 `opay-google-picvid-1000-backfill-20260827.service` 成功退出0；MySQL全程通过只读63350入口，不写源库。
2. 发布前发现BUG-GCP-003：新图片视频消耗不能配全Campaign AF。`c3a39dc040367c0c7ee730d5ccf0df1ba5f93717`修复平台AF混用，使用已刷新独立缓存重建全部七个月快照。旧候选没有公开发布。
3. 最终影子版本 `20260827T173114606659+0800`，latest SHA256 `9536d55f31751657b67395a2d75cb000c23fb7d0aba45e801c01b7d140de7247`。独立raw-cache验收、审批46条、Meta/TT186条全字段守恒及14份CSV通过后才正式切换。
4. 正式七个月JSON与已验影子除data_version外逐字段完全一致。HTML/latest/七个月JSON共9个匿名请求均200；noindex、latest no-store、月份immutable已验证。
5. 浏览器收尾发现CPA缺失说明沿用旧“USD基准不完整”。`295b14d`仅修正新GG政策的说明为“图片/视频素材池无同口径AF数据”，新增前端测试。GitHub推送并按SHA发布；latest和七个月JSON逐字节未变，缓存和选优未重跑。

### 测试、浏览器和证据

- 最终运行提交本地及服务器：`python -m unittest discover -s ops/opay-excellent-creatives -p 'test_*.py'` → 152/152；`python ops/opay-excellent-creatives/validate_frontend_contract.py` → 50/50行为用例、语法及契约通过。
- 七个月完整raw-cache独立重算PASS；46条7月审批样本逐ID、App、类型、基础数据及A/B标记PASS；Meta/TT186条全部原字段、基准、审计和旧cache事实表哈希守恒PASS。
- 最终页面内联JavaScript逐月执行真实JSON的导出，14份全部/Google CSV通过，34列、空值及精度一致；334/334行源文件和缩略图均available。
- Chrome线上实际操作确认46条、NG40/PK6，规则筛选8/28/10。素材2072578图片1200×1500正常；4568515视频1280×720、10.1秒、readyState4、error=null。详情CPC/CTR、AF留空及最终原因文案通过。
- 390×844响应式视口：文档clientWidth=scrollWidth=375，表格容器334、内容2260；键盘横滚scrollLeft从156到166，页面无横向溢出；已恢复原视口。
- 1月GG审计0条及NG历史汇率缺口可见，无GG占位行。浏览器最终停留在7月Google/全部App/全部规则46条。
- 原生CSV下载事件等待12秒仍未捕获；不宣称浏览器文件保存位置已验。真实导出Blob/CSV内容已验证通过，此项是浏览器环境验收限制，不是已发现的内容错误。
- 在独立影子目录注入latest提交失败，旧manifest不变；演练恢复旧HTML/current并确认旧七个月仍可读、新文件保留。没有为测试故意把生产切回旧规则。

所有服务器证据位于 `/mnt/data-disk/opay-excellent-creatives/qa/acceptance-20260827-google-picvid-1000`：

- `release-final.json`：最终295b14d运行提交、HTML哈希、数据字节未变及补丁备份。
- `promotion-result-final.json`、`promotion-operation-final.json`：17:33 c3a39dc主发布、9个HTTP、月度统计、旧版本和timer状态。它们保留原始发布事实，最终补丁版本以release-final为准。
- `production-seven-month-reconciliation.json`、`seven-month-reconciliation-final.json`：生产/影子独立核算及46条审批清单通过。
- `backend-tests-release.log`、`frontend-tests-release.log`、`frontend-release-2026-MM.log`、`csv-release/`：最终152/50及14份CSV。
- `browser-acceptance.json`、`health-final.json`：实际UI、缺失项、主站/报表HTTP、nginx测试、timer。
- `rollback-drill-result-final.json`、`ready-final.json`、`af-scope-rebuild.json`、`backfill.log`、`baseline-public/`：失败演练、切换门禁、缓存重建、源刷新及冻结旧基线。

## 备份和精确回滚程序

全量回滚点为旧规则72f2e744、旧数据版本 `20260827T152235588279+0800`。备份目录 `/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-picvid-1000`，manifest SHA256 `1d1d0a247f39080e896c36a2842bba3fdc0ef5a4fa6693d860def55f9c4039c7`，含旧HTML/latest/七个月JSON、配置及旧cache一致性备份。旧release和原旧cache仍原位保留。

另有仅前端提示补丁的回滚点 `/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-af-label-295b14d`，对应c3a39dc运行代码与同一新口径数据版本。以下程序是**完整撤销本轮新规则**，不是仅撤销提示文字。

在43.166.187.96以有权管理本报表的账号执行以下命令。必须先取得回滚授权；任一版本/哈希/在途检查失败立即停止并重新评估，不能移除断言强行运行。不覆盖缓存、不删版本，不改其他报表，不需Nginx重载。

```bash
python3 - <<'PY'
import fcntl, hashlib, json, os, sqlite3, subprocess, tempfile, uuid
from pathlib import Path
web = Path('/usr/share/nginx/html/reports/opay-excellent-creatives')
current = Path('/opt/opay-excellent-creatives/current')
backup = Path('/mnt/data-disk/opay-excellent-creatives/backups/20260827-pre-google-picvid-1000')
expected = Path('/opt/opay-excellent-creatives/releases/295b14d162b85a397e02dfcdbd637ef4a497e4b5')
old = Path('/opt/opay-excellent-creatives/releases/72f2e7440e16d8d3ea782ce9eea31176d21c0797')
timers = ['opay-excellent-creatives-initial.timer', 'opay-excellent-creatives-final.timer']
def sha(p): return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def active(u): return subprocess.run(['systemctl', 'is-active', u], capture_output=True, text=True).stdout.strip()
def atomic(path, data):
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '.rollback-', dir=str(path.parent))
    with os.fdopen(fd, 'wb') as f:
        f.write(data); f.flush(); os.fsync(f.fileno())
    os.chmod(name, 0o644); os.replace(name, path)
def point(target):
    temp = current.parent / ('.current-rollback-' + uuid.uuid4().hex)
    temp.symlink_to(target); os.replace(temp, current)
with open('/tmp/opay-excellent-creatives.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert current.resolve() == expected
    assert sha(web/'latest.json') == 'fbb6e5990d1dbab09273ad6ce0834f679603466c5943814ea26cac335dcda95c'
    assert sha(web/'index.html') == '803991e83d8c1aeaa6e9997ca7fa7d78b5888da056aff25b60a15197fb05d472'
    assert sha(backup/'manifest.json') == '1d1d0a247f39080e896c36a2842bba3fdc0ef5a4fa6693d860def55f9c4039c7'
    m = json.loads((backup/'manifest.json').read_text())
    for original, entry in m['files'].items():
        assert sha(entry['backup']) == entry['sha256']
        if original.startswith('/etc/') or '/data/' in original:
            assert sha(original) == entry['sha256']
    assert old.is_dir()
    assert subprocess.check_output(['git','-C',str(old),'rev-parse','HEAD'],text=True).strip() == old.name
    db = sqlite3.connect('file:/mnt/data-disk/opay-excellent-creatives/cache/opay-excellent-creatives-google-cpc.sqlite3?mode=ro', uri=True)
    assert db.execute('PRAGMA quick_check').fetchone()[0] == 'ok'; db.close()
    for stage in ['initial', 'final']:
        assert active('opay-excellent-creatives-refresh@' + stage + '.service') == 'inactive'
    assert all(active(t) == 'active' for t in timers)
    forward_html, forward_latest = (web/'index.html').read_bytes(), (web/'latest.json').read_bytes()
    try:
        subprocess.run(['systemctl','stop',*timers],check=True)
        atomic(web/'latest.json', (backup/'latest.json').read_bytes())
        atomic(web/'index.html', (backup/'index.html').read_bytes())
        point(old)
        assert sha(web/'latest.json') == m['old_latest_sha']
        assert current.resolve() == old
    except BaseException:
        atomic(web/'latest.json', forward_latest)
        atomic(web/'index.html', forward_html)
        point(expected)
        raise
    finally:
        subprocess.run(['systemctl','start',*timers],check=True)
        assert all(active(t) == 'active' for t in timers)
    print('Rollback complete; verify anonymous HTTP and browser before closing.')
PY
```

回滚后匿名检查HTML/latest及旧版本七个月文件200，7月GG回到旧6条；执行`nginx -t`及两timer状态核对。新旧release/cache/data均保留，不执行删除或覆盖数据库。上述生产回滚程序未实际执行；已执行的是隔离影子失败/回滚演练。
