# 开发计划

隔离分支codex/meta-video-delete-credentials-20260915，基于ede6316；生产基线1b86d2af。

分工：Graph/Source/Bridge及独立凭证测试；前端诊断及DOM模拟；Service/Store预写审计、线上只读关系调查、规范文档及部署。所有人员各自限定文件所有权。

流程：只读身份调查→需求/SA→实现与测试并行→独立复查→本地编译回归→GitHub提交推送→服务器精确检出/只读凭证probe→备份并排空→双静态切换及主API重启→只读验收。

验证命令：
python -m unittest discover -s tests -p test_fb_ad_asset_delete_*.py -q
python -m py_compile features/fb_ad_asset_delete/graph.py features/fb_ad_asset_delete/source.py features/fb_ad_asset_delete/bridge.py features/fb_ad_asset_delete/service.py features/fb_ad_asset_delete/store.py scripts/deploy_meta_video_credentials.py scripts/meta_video_credential_probe.py
node --check static/fb-post-ad-delete.js
git diff --check
