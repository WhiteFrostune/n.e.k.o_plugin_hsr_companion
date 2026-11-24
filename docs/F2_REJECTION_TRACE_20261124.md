# F2 拒绝记录本（2026-11-24）

基线：F1 / PR #24 / `02610e6`。分支：`codex/0.9.8-f2-rejection-trace-20261124`。仅记录，不安装、不实机、不录帧；不修改识别、调度、pending 关系或补答判断。F3 未实施。

## 核心改动

- `runtime/rejection_trace.py`：256 条事件环，最近 8 个请求摘要。保留首拒、末拒、最后事件；记录覆盖数和记账错误数。单次故障不抛入业务。短锁只包追加/复制，无 I/O、await、识别任务或重试。
- 同一请求、任务/帧、检查、结果和原因重复时合并 count/first_at/last_at。其他检查穿插不挤掉同一拒绝；实际结果变化仍另开事件。请求摘要和导出均为脱离内部状态的副本。
- 默认仅枚举、随机/已有请求 ID、任务/帧序号、时间及数值耗时。不接收 OCR、角色名、聊天、UID、路径、凭据或内容散列；未知枚举为 unmapped_code 并计数。没有独立测得耗时的分支 duration_ms=null，不能把它解释成 0ms；各操作段耗时仍取 F1 的 2F query_timings。
- Q14 的 allow/admitted 只表示该工具结果获准返回，不代表消息已提交或猫娘已发言；X06 的 submitted 才是原 SDK 提交回执，仍不是宿主播放见证。
- 分层：新记录本归 trace 旁栏；只在 layers.toml 登记新文件，layers-baseline.toml 不变。不引入新任务系统或识别工作者。
- schema 2 仅保留元数据、floors、大小控制；checks / last_query / observation_health / query_timings 等旧顶层键删除。面板、复制与 JSON 导出使用同一 diagnostic_id。拒绝事件按楼层放入 recent_events，4F.metrics.rejection_trace 存环形指标和 8 份请求摘要。
- 首次启用账本增加 auto_restore：初始化终态以实际 startup 返回/异常/取消为准，不提前宣告完成。重复连接结果合并；不改变恢复流程。
- 面板仍为五行摘要、逐层详情、一个复制按钮。诊断/导出失败保留同一快照的可复制文本；复制失败警告只在复制真的失败时显示。
- UTF-8 目标 96KiB、上限 128KiB。超目标先减少可选事件细节并记 omitted_events；超上限返回完整 size_limited JSON，不截断字符串。请求摘要在常规裁减中保留，极端整体降级显式标 details_omitted。

## 编号 → 文件:函数 → 记录内容

以下编号为本轮部分覆盖，不代表同编号在方案中的全部内部检查都已插桩。尤其未新增 L2 原生 OCR 内部插桩。

| 编号 | 文件:函数（当前行号） | 记录原因/结果 |
|---|---|---|
| B01/B02 | `runtime/delivery_boundary.py:reject` (79) | `reason` |
| B05 | `runtime/delivery_boundary.py:review` (235) | `'fields_withheld'` |
| B06 | `runtime/observation_store.py:matches` (123, 130) | `'fields_withheld', 'identity_withheld'` |
| D02 | `runtime/live_refresh.py:validated_delivery` (1237, 1243, 1247, 1260, 1264, 1267, 1270, 1253) | `'epoch_changed', 'not_observable', 'proof_rejected', 'receipt_rejected', 'revision_changed', 'source_unconfirmed'` |
| F02 | `runtime/live_refresh.py:_guarded_analysis` (633, 639) | `'analysis_superseded', 'preempted'` |
| F07 | `runtime/live_refresh.py:_guarded_analysis` (646, 663, 673) | `'analysis_superseded', 'parsed', 'yielded'` |
| F08 | `runtime/live_refresh.py:_guarded_analysis` (685, 723, 716) | `'analysis_superseded', 'capture_failed', 'epoch_changed'` |
| F09 | `runtime/live_refresh.py:stable_screen_content` (267) | `'fields_withheld'` |
| F10 | `runtime/live_refresh.py:_guarded_analysis` (779, 756) | `'controls_changed'` |
| F12 | `__init__.py:_observer_loop` (1340, 1364) | `'capture_failed', 'no_analysis'` |
| F13 | `__init__.py:_observer_loop` (1379) | `'late_result'` |
| F14 | `__init__.py:_apply_local_analysis` (778) | `'incomplete_read'` |
| F14 | `__init__.py:_observer_loop` (1426) | `'incomplete_read'` |
| F16 | `__init__.py:_apply_local_analysis` (832) | `'incomplete_read'` |
| F18 | `__init__.py:_apply_local_analysis` (767, 948) | `'commit_revoked'` |
| F20 | `__init__.py:_apply_local_analysis` (953, 981) | `'committed', 'full_scope_missing'` |
| P01 | `runtime/review_policy.py:checked_receipt` (70, 73, 78, 86, 90, 83) | `'fields_missing', 'frame_stale', 'generation_changed', 'invalid_timestamp', 'not_observable', 'snapshot_rejected'` |
| P02 | `runtime/review_policy.py:review_current` (100, 103, 108, 127, 123) | `'missing_proof', 'not_observable', 'preview_pending', 'source_unconfirmed'` |
| P03 | `runtime/review_policy.py:review_current` (140) | `'superseded'` |
| P04 | `runtime/review_policy.py:producer_receipt` (158, 161, 164, 167, 170, 176, 179, 182, 185, 188, 191, 194, 197, 200, 203, 206, 211, 216) | `'answer_generation_missing', 'capture_failed', 'deferred', 'frame_stale', 'generation_changed', 'invalid_timestamp', 'missing_proof', 'no_plan', 'not_observable', 'packet_revision_changed', 'pending_unproved', 'preview_pending', 'proof_epoch_changed', 'proof_revision_changed', 'raw_revision_changed', 'refresh_generation_changed', 'refresh_generation_missing', 'source_unconfirmed'` |
| P05 | `runtime/review_policy.py:producer_receipt` (227) | `'snapshot_rejected'` |
| Q01 | `runtime/current_regions.py:read_current_character` (70) | `'unsupported'` |
| Q02 | `runtime/current_regions.py:prepare` (86, 89) | `'no_source', 'not_live'` |
| Q02 | `runtime/current_regions.py:read_current_character` (98, 101, 104, 107, 110) | `'deadline', 'no_analysis', 'not_live', 'scene_unverified', 'wrong_page'` |
| Q10 | `runtime/current_regions.py:read_current_character` (114, 125, 128, 131) | `'no_controls', 'no_frame', 'not_live', 'window_changed'` |
| Q11 | `runtime/current_regions.py:read_current_character` (143) | `'controls_changed'` |
| Q12 | `runtime/current_regions.py:read_current_character` (152, 155) | `'epoch_changed', 'not_live'` |
| Q13 | `runtime/current_regions.py:read_current_character` (179, 182) | `'deadline', 'not_live'` |
| Q14 | `runtime/current_regions.py:read_current_character` (206, 192, 195, 198, 201, 204) | `'admitted', 'no_result', 'not_live', 'receipt_rejected', 'snapshot_rejected', 'superseded'` |
| V07 | `runtime/query_reuse.py:review_snapshot` (383, 391, 420, 424, 400, 415) | `'candidate_missing' if not snapshot else 'observation_invalidated', 'epoch_changed' if snapshot['epoch'] != epoch else 'invalid_timestamp', 'expired', 'invalid_timestamp', 'missing_proof', 'observer_not_observing'` |
| V08 | `runtime/query_reuse.py:review_snapshot` (428, 432, 440, 448, 465, 460, 456) | `'capture_failed', 'no_client' if client is None else 'no_pid', 'stale_not_allowed', 'superseded'` |
| V09 | `runtime/query_reuse.py:review_snapshot` (478, 491, 501) | `'busy', 'native_review_deferred', 'superseded'` |
| V10 | `runtime/query_reuse.py:review_snapshot` (510, 523, 526, 530, 534, 543) | `'frame_stale', 'no_frame', 'proof_rejected', 'superseded'` |
| V11 | `runtime/query_reuse.py:review_snapshot` (566, 587) | `'deferred', 'retained_proof_rejected'` |
| V12 | `runtime/query_reuse.py:review_snapshot` (614, 620, 606) | `'controls_changed', 'insufficient_controls', 'missing_proof'` |
| V13 | `runtime/query_reuse.py:review_snapshot` (628, 649) | `'invalidated_during_check', 'pixels_changed'` |
| X01 | `__init__.py:hsr_companion_prepare` (2465) | `'pending_intercepted'` |
| X01 | `runtime/pending_handler.py:record` (18) | `reason` |
| X04 | `runtime/followup_delivery.py:expire` (338) | `reason` |
| X05 | `runtime/chat_activity.py:latest_user_activity` (101, 152, 158, 140) | `'activity_busy', 'cancelled', 'timeout' if isinstance(exc, TimeoutError) else failure_code or 'activity_error', 'unsupported_activity'` |
| X05 | `runtime/followup_delivery.py:checked_activity` (199, 165, 169, 172, 175, 179, 182, 193) | `'activity_retry', 'deadline', 'observer_unavailable', 'relation_hold', 'request_replaced', 'retry_exhausted', 'target_changed'` |
| X06 | `runtime/followup_delivery.py:_cancel_current` (138) | `'request_replaced'` |
| X06 | `runtime/followup_delivery.py:cancel_followup` (123) | `reason` |
| X06 | `runtime/followup_delivery.py:deliver_followup` (628, 598, 554, 557, 560) | `'cancelled', 'epoch_changed', 'observation_invalidated', 'snapshot_rejected', 'submitted' if submitted else 'submission_failed'` |

## 尚未插桩 / 不能据此下结论

- Q03–Q09、Q15–Q18：原生工作者、姓名/框/区域内部的具体拒绝。
- V01–V06、V14–V16：原生文字证明、scope 内部和协作核验任务调度细节。本轮 V07–V13 在缓存出口留痕，没有替代这些内部证据。
- B03–B04：逐事实/角色的详细扣留原因；现阶段 B05/B06 记录部分扣留及最终匹配失败。
- D01、D03：兼容最终核验、整体提问循环内部回退细节。
- F01、F03–F06、F11、F15、F17、F19、F21–F22：未开始/原生读取、过程探测/排空、guard 细节、目录过滤、身份状态机、store 内部、提交后投影、巡逻细节。本轮保留既有 E1/E2 账本，未改这些判断。
- H01–H08：切页/工作者内部拒绝；X02–X03：规划及直答组装内部。
- 已插桩的复合 guard 仍有按检查组记录的部分：不会据一个 group code 推测究竟哪一像素区域、哪一原生 OCR 子项失败。本轮未录帧，不能追溯重建用户之前截图。
- 账本只在内存，进程/插件实例重建即失去旧账本；跨重启由未授权的 F3 决定。

## 验证与边界

新增测试覆盖快读真实出口、回执各短路原因、pending 新旧请求关系、聊天活动 unsupported/busy/SDK Err/异常/超时/取消、补答重试/取消、快照单次许可、环形与重复合并、auto_restore 成功/异常/取消、隐私、唯一快照/导出/UTF-8 大小。

对 full、cache、boundary、observer 与 commit 做插桩擦除后的 AST 对照，检查原条件、return、await、异常结构一致；短路展开的快读与回执另断言前序失败不会调用后序 matches。记录器故障下保持原返回和异常/取消。

旧测试只迁移楼层读取路径及旧兼容断言到新 schema；没有删正确性断言、放宽期限或姓名要求。测试使用的 floor helper 不提供旧顶层回退。

保留每轮定向失败记录。首轮全量：2756 通过 / 10 跳过 / 0 失败；后续记录精度和生命周期收尾后的最终三道门以本报告下方实际结果为准。新增 observer 替身出现无退出测试循环，已按 PID 停止测试进程并改为可控取消；没有改产品循环来配合测试。

既有 Node S4 待办：主动搭话 Card 断言失败（30 项中 29 通过 / 1 失败），未修改该断言或产品卡片。

## 假数据复制示例

完整示例见 `docs/examples/F2_DIAGNOSTIC_FAKE_COPY.txt` 与 `.json`。开头为 5 行摘要，随后完整 JSON；所有 ID、时间、版本与拒绝记录均为合成数据。不能把示例视为本机实测。

## 最终门结果

| 门 | 本次实际结果 |
|---|---|
| 全量 pytest，seed=11222026 | 2787 通过 / 10 跳过 / 0 失败，2797 项，245.697 秒 |
| Ruff check . | 通过，0 问题 |
| 分层检查 | 5 通过 / 0 失败；import 越界 0，baseline 零新增且文件未变 |
| 补充 Node 面板测试 | 29 通过 / 1 既有 S4 失败；该主动搭话 Card 断言未改 |

新增 F2 测试 153 项。最终全量实际观察到 39 个已插桩编号、150 组调用点/原因见证；这不是 84 个规划编号全覆盖，也不等于每个编号内全部内部条件已记录。测试原始报告保留本机 logs，不作为包含路径/主机名的原始 XML 上传。可发布的结果和见证摘要为 `docs/F2_VALIDATION_20261124.json`。

旧待办继续保留：首次手动开启陪玩总失败、需重载；面板版本显示 0.9.7；主动搭话 Card 的 Node 失败。F2 仅为第一项补记录，不修流程；其余仍归 S4。

实际不安装、不调用实机服务、不进游戏、不录帧。未改 8 秒预算、姓名门槛、15/60 秒保鲜期、70 秒终点和 350 毫秒窗口。
