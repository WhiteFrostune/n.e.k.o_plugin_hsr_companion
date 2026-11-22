# S1 测试隔离修复（D 前置）

父提交 cfc5dc4；本提交只改测试、加入离线分析脚本及本记录，不改产品。

原失败：先执行 test_late_relic_question_does_not_wait_for_unrelated_line_reread[complete]，再执行 test_no_commit_reads_current_character_and_keeps_real_capture_time[fields0]，为 1 通过/1 失败。current_regions 惰性导入时把 live_refresh 的替身保存为模块别名，monkeypatch 只恢复定义模块。

修复：测试在替身安装前导入两个模块，并通过 patch_validation_capture 同时注册定义路径与消费路径，均由同一 monkeypatch 作用域恢复。增加作用域退出后双路径恢复原对象的测试。旧断言、产品接口和等待门槛均未改变。

定向：原遗器用例全部参数、角色快读、姓名确认保护，固定原污染先后顺序，42 通过。原红灯 XML 保留于 E:/code/neko2/logs/s1-bottleneck-order-red-20261122.xml；修后 XML 为 s1-isolation-targeted-20261122.xml。

随本提交加入 analyze_ocr_bottlenecks.py：上一轮已执行、未提交的纯离线分析脚本。对照与无效样本说明仍见 S1_BOTTLENECK_OPTIONS_20261122.md。本轮不执行任何产品行为变更、不安装、不实机。

全量采用显式随机种子11222026，保留随机顺序覆盖，不通过禁用随机顺序来使全量变绿。三道门结果待本轮执行后填入。

首轮全量发现另一项测试数据时钟依赖：隐私断言禁止38.8，却命中checked_at的38.888秒。产品/断言未变，只用既有now参数固定该测试时间。首轮红灯XML保留：s1-isolation-full-20261122.xml；修复后用同一随机种子复验，不以无修改重跑取绿。

最终三道门：全量2579通过/10跳过/0失败，种子11222026；Ruff E4,E7,E9,F,I通过；分层5通过/0越界。AST对照确认两份旧测试里的原断言均未改变。无产品源码差异。
