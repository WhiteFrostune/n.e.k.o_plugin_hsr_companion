# Q11 控件变化文字复核兜底（2026-11-25）

基线：F2 a4e1b29。分支：codex/0.9.8-q11-control-reread-20261125，堆叠在 F2。

## 最终改动

- runtime/current_regions.py：只替换 Q11 的“像素变化就拒绝”。只复核实际变化的控件，调用原 LocalVisionAdapter.verify_text_regions；仍由 run_query_worker 使用同一调度器、适配器和原截止时间。复核成功后继续原有快照/姓名/字段/收据审核，保留 source.observed_at；不把校验帧的名字/数值拼入旧包。
- runtime/query_reuse.py：把现有像素规则的第一失败项提取为纯结果，regions_changed 仍返回相同布尔。没有修改 .015、.02、24、64、8×8 或任何几何/空数据检查。
- runtime/rejection_trace.py：仅为 Q11 增加 q11{pixel_rule,region_index,region_purpose,text_review} 的封闭字段，投影进现有 4F/单份复制。未知枚举映射 unmapped_code，不记录 OCR/角色名/图像/散列/对话。不同框或复核结果不会被合并成同一条。
- tests/test_q11_control_reread.py：新增反例和保护测试；没有改任何旧测试或断言。

完全复用现成原生复核：control ≥.85；2/3/4 放大倍数中两次精确一致且无高置信矛盾；最多 16 条；尺寸/坐标限制照旧。两次放大是原有核验，不是新增失败重试。

没有修改 F10、UI、补答/RPC、版本号、8 秒预算、姓名门槛、15/60 秒、70 秒终点、350 毫秒窗口或新帧 ≤2 秒约束。没有新增睡眠、重试、第二 OCR 实例/工作者。现有工作者取消/保留/排空机制继续生效。旧源码与安装版不更新。

## 证据和测试范围

未修产品代码先跑新增 6 项：4 passed / 2 failed，两个失败均为字不变但框内光效导致 Q11 拒绝，原失败 XML 留在本机 q11-red-20261125.xml。

开发中一个新增“16 条上限”用例因夹具没有显式 _observation_epoch 属性失败；按生产同样的缺省 0 修正了新夹具访问，未动任何正确性断言。导入排序由 Ruff 修正。保留了开发失败回执。

定向最终回归：269 passed / 0 skipped / 0 failed，11.139 秒（seed 11252026），覆盖新增用例及 F2、当前角色、姓名、缓存、像素阈值保护。

新增覆盖：框内发光且文字一致、实际文字改变、空读/弱读/高置信矛盾；原审核继续执行；未确认名字不成为答案；一位属性数字变化不得沿用旧值；过期/新问题/epoch/采集失效/暂停；调度不可用/原剩余期限耗尽；原 16 条和放大次数；真实复核串行；诊断故障/最小记录隐私/单份复制/框编号合并。

这些测试使用合成帧和受控 OCR 返回，真实调用现有文字核验与快照函数；不是实机 OCR 准确率或端到端 8 秒成功率证明。本轮未安装、不实机、不录帧。

## 提交前门禁

- 全量 pytest（seed 11252026）：2826 passed / 10 skipped / 0 failed / 0 errors，2836 项，209.691 秒；其中新增 Q11 用例 39 项。没有改旧断言。
- Ruff：exit 0，All checks passed。
- 分层门：5 passed / 0 skipped / 0 failed，1.238 秒；layers-baseline.toml 未变、零新增。
- Release check：exit 0，289.015 秒，tests=passed，打包及 payload_hash_verified=True。在独立的当前 CI main 1c67f33d96b4915c2f55bb93062b1dc8a20e56b3 下运行原命令 uv run python -m plugin.neko_plugin_cli.cli check -r hsr_companion；成功输出不含内部 pytest 数字，不推算。本地 Windows / Python 3.11.15，复用冻结依赖、UV_NO_SYNC=1，不改运行中宿主或其依赖；不声称操作系统与 GitHub Linux runner 相同。

## 代码定位

- [current_regions.py:87](../runtime/current_regions.py#L87)：已有复核器的有界单工作者调用。
- [current_regions.py:208](../runtime/current_regions.py#L208)：变化框选择和 Q11 后备复核，成功仍走下游原审核。
- [query_reuse.py:114](../runtime/query_reuse.py#L114)：原像素算法首个失败项；150 行保留原布尔接口。
- [rejection_trace.py:169](../runtime/rejection_trace.py#L169)：最小记录封闭清洗；248 行写入、357 行同一快照导出。
- [test_q11_control_reread.py](../tests/test_q11_control_reread.py)：39 项新增用例。

## 诊断示例（假数据）

```json
{"branch_id":"Q11","reason":"controls_changed","q11":{"pixel_rule":"cell_cluster","region_index":0,"region_purpose":"page_control","text_review":"contradiction"}}
```

region_index 为原 scene_regions 的零基序号；无有效对应框时为 null。用途只记录 page_control，不猜它是哪段文字。省略的框/像素/内容/帧细节不冒充完整 A 方案。只在 Q11 拒绝时追加该字段；复核成功不制造一条“拒绝”记录。

提交前确认：运行 Release check 的三个产品文件及新增测试文件，与当前候选字节一致。F10、原生复核器、调度器、预算/版本常量、UI、分层白名单和所有旧测试均未改动。远端 CI 如有结果，在草稿 PR 中另列，不用本地结果冒充。
