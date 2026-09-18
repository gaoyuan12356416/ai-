# 部署和回滚

部署前快照：CPU43.166.187.96，HK43.154.250.89。Drama a93b541；TT random-current实际HEAD16ae57a；FB e3bef98。CPU不替换业务代码，FB不替换代码。

旧目录SHA：028326ab211418934b026c227f2e3707553cce7560551dca3c0bfddc681d566f。
新目录SHA：24d6fad3174f765d3433c9ab71322f04241b11acf0224a4e1392f138295adf0c。

先完成本地测试与GitHub push，服务器fetch精确提交；离线stage素材并验证两套缓存，随后保存配置/指针，暂停TT入口和七个触发器、FB prepare.timer，排空活动制作后重启对应worker与tunnel。CPU最后切新manifest并窄重启主API/job-worker，恢复全部触发器。

回滚采用兼容代码保留、新旧目录均保留、仅恢复原默认配置。若已有新SHA冻结任务，不得退回无多目录兼容的代码。无数据库回滚，不恢复历史账本，不删除检查点、下载或成片。实际提交/备份/命令/验证证据在部署完成后补齐。
