# Post 素材产品范围

FB、TikTok、YouTube、X 的素材查询不再以 Dramawave 产品字段作为发布资格。
素材产品通过 `ads_apps_setting.name → id` 查到对应剧库 app，剧名、简介、语言及投放时间都在该 app 下读取。
原有空产品及社媒通用素材标签继续沿用原 Dramawave 映射。重复产品名使查询明确失败，不任意挑选 app。
X 素材选择支持全部产品、Moboreels、MoboShort，精确 ID 仍可跨产品查询。
YouTube 的独立素材 SELECT 改为全部视频产品，素材 app 与频道授权 app 分开处理。

显式 `{{m_url}}` 继续使用本机构当前上架的 Moboreels 剧库及冻结创建者，不改写旧 URL 宏。
Moboreels 自动选材去除旧固定素材产品条件，保留机构、上架、来源、语言、时长和原有任务防重。
普通自动模板中用户明确配置的选材和指标条件保持原义；不把 Dramawave 广告指标用于其他产品。
账号授权、媒体规则、租约、提交幂等、未知回执与历史任务均保留。

## 验证

`tests/test_post_product_scope.py` 验证跨产品同剧 ID 隔离、上下文恢复、缺失映射、FB 元数据、YouTube 素材及所有部署版本的 X/TT 校验器。
现有 Moboreels 社媒、FB、YouTube、资格测试继续覆盖宏和冻结回执。
`verify_readonly.py --root CANDIDATE --lane main|fb|tt|tt_auto --material ID` 只读验证真实素材，不创建推广、任务或帖子。

## 部署和回滚

GitHub 提交推送后，在 CPU fetch 精确 SHA 并创建独立 checkout。
`/usr/bin/python3 RELEASE/ops/post-product-scope-20261113/deploy.py SHA stage` 检查当前路径和逐文件摘要、备份并建立候选。
按 `x`、`fb`、`tt`、`youtube` 分别执行 `pause`、`busy`、`activate`、`resume`，均附 `--deployment DEPLOYMENT --group GROUP`。
仅暂停该渠道新领取，等待其在途执行自然结束，恢复原有调度触发器。
`youtube` 阶段同步主 API 与静态文件、YouTube 素材 SQL，并重启受影响的 API/YouTube 拥有者。
回滚同一脚本依次执行 `rollback-pause`、`busy`、`rollback`。保留最新业务库、短链、回执和凭据；不恢复旧库、不补发历史任务。
候选版本与备份路径记录于返回的部署 manifest，运行目录漂移须重新核对后合并。

本次 FB 排空期间，另一部署 `20281127` 更新了 Page 池混选。`rebase_fb.py` 逐文件核对已评审的三项改动，将本次四个素材文件叠加到该运行版本，并保留独立回滚与原调度快照。其余渠道使用原始部署版本。
