# 设计评审

通过，以下发现已纳入实现：
- Moboreels 生产剧库存在 cover，但所有分集地址为空，必须提供封面元数据路径。
- 共用产品 API 还服务视频页面，改为封面专用接口，复用 cover_synthesis 权限。
- 产品 2373 的旧 package 字段并非实际 Android 包名，上传使用已核实 google_app_android=com.changdu.moboshort；3360 使用 com.changdu.mobovideo。
- 原自动脚本 SQLite 出错返回空集合，会放行重复提交，改为只读查询并失败关闭。
- 保留 DramaWave 原有四舍五入比较口径；新产品严格比较 SUM(spend)>3000，不设 ROAS 门槛。
- 生产 app.py 是复合版本，部署只替换两个函数和加入一个 GET 分支，其他顶层函数 AST 必须完全一致。
