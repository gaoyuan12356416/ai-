# X 手动发布：多个素材原始 ID 搜索

选择素材输入框支持空格或半角逗号分隔原始 ID，也支持混合分隔和去重。去重后最多 200 个，与素材选择上限一致。批量 ID 走一次有界精确查询，不进入名称模糊扫描；80 字限制只适用于名称/剧名搜索。精确 ID 保留跨产品、语言筛选与已有素材校验，结果按数字 ID 倒序稳定翻页。单条素材无法通过原有校验时仍按原行为跳过。

验证：`python -m unittest scripts.test_x_video_publish_gateway -q`；`node tests/x_video_publish_frontend_test.cjs`；`node --check static/x-video-publish.js`。上线后运行 `python3 scripts/verify_x_material_multi_id.py --ids <ID1> <ID2> <ID3>`，只读核对逗号、空格、混合分隔和公开静态文件。

部署前先推送 GitHub，服务器从该分支检出精确提交。运行 `python3 scripts/deploy_x_material_multi_id.py`；脚本核对三份相关文件与线上基线，备份到数据盘，只更新查询模块与页面 HTML/JS，重启 `drama-material-api.service`。X 发布及 Auto 服务 PID 必须保持不变，不操作任务、账本、凭据或调度。

回滚：从本次 GitHub checkout 运行 `python3 scripts/deploy_x_material_multi_id.py --rollback <backup>/manifest.json`。仅恢复此次文件并重启查询 API；若后续改动已覆盖这些文件，会拒绝覆盖，需先合并保留后续修改。当前账本和业务事实不回退。
