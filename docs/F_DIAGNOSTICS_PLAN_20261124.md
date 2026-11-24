# F：按楼层重构一键诊断的实施方案（2026-11-24）

状态：仅核实与规划，等待批准。本文不代表已实现或已通过验收。

基线：E2 / PR #23，`596385d30363e095c724a362f59adfa1786c799a`，分支 `codex/0.9.8-e2-patrol-confirmation-20261122`。代码位置均以该提交为准，后续实施要随 diff 校正。

工作目录：`E:\code\neko2\s1-host\plugin\plugins\hsr_companion`。本轮仅新增本文，不修改产品、测试或配置，不安装、不录帧、不提交、不推送、不创建 PR。

## 0. 目标、边界与验收产物

F 只让下一次失败能够回答“哪条检查，在什么时刻，扣住了哪份结果”。不修识别、不调整调度、不放行任何候选，不用新增诊断替代原有业务判定。

硬约束保持：原 8 秒提问预算；姓名确认门槛、次数及当前帧证明；15/60 秒保鲜；新帧最大 2 秒；70 秒确认终点；350 毫秒可选补充窗口；原 `observed_at`；换页、换角色、换件及完整词条保护。F 不调整补答重试、期限或主动消息策略。

允许未来实施的产品变化仅是诊断与面板的呈现、记录和复制：同一份诊断快照，5F→1F 摘要和细节，一键复制全部，有限拒绝记录本。诊断失败不能改变识别返回值、异常、取消、超时或提交路径。

本轮 E2 证据来源：`E:\code\neko2\install-runs\e2-pr23-20261122-230318\acceptance-20261124\E2_LIVE_ACCEPTANCE_20261124.md` 及其逐次 JSON。12 次身份提问中 4 次答对、8 次未确认，场景前提不完全一致，不能当长期成功率。`result_ready=true` 不等于宿主收到正文，更不等于猫娘已开口。

### 楼层展示与真实代码分层

用户指定的诊断楼层是排障视图，不重新定义 `layers.toml`：

| 诊断楼层 | 本文展示职责 | 当前真实归属 |
|---|---|---|
| 5F | 工具、规划、待办、补答、面板见证与交付 | entrypoints + L3 规划 + L5 交付；L4 分析结果仅以完成状态展示 |
| 4F | 观察、巡逻、身份状态、候选核验、拒绝记录本 | L2b + entrypoints 装配；不是把运行时改归 L4 |
| 3F | 页面/姓名/事实检测器、区域计划 | L2a |
| 2F | OCR 原生调用、锁/队列、慢块 | L1；调度时序由 L2b 提供关联事件 |
| 1F | 构建、插件进程、采集助手连接和生命周期 | contracts/entrypoints/L1 |

`layers-baseline.toml` 当前越界白名单为空。F 不改这些模块的现有归属，不增加白名单，不让低层导入诊断装配入口或 UI。

## 1. 现状盘点

### 1.1 一键诊断的入口与各块来源

入口在 `__init__.py:3164–3200`。`diagnostic_reader.py:7–27` 并行读取助手状态、工具审计和当前视觉证据，各自 1.25 秒等待，失败记 `incomplete_sources`；不是发起 OCR。随后入口还单独检查消息路径、读取 prepare 失败记录，这些步骤不在前三个来源的超时包装内。

| 现有输出 | 数据来源及代码位置 | 限制 |
|---|---|---|
| `checks`、`checked_at`、版本/构建 | `runtime/diagnostics.py:153–196,270–305`，build_info、observer、assistant、资料状态、message_path | 一组健康徽章，不等于整条请求成功 |
| `service_request`、`prepare_calls` | 入口 `_service_request`；`prepare_trace.read_records`，`__init__.py:3184,3194` | prepare 最近 4 条；只有失败尝试持久化，成功主要在内存 |
| `review` | `review_policy.review_status:29–39` | 有候选、代次、待核验类别和数量；无具体拒绝谓词 |
| `observer`、`assistant`、`resources` | 观察状态、助手健康、资料包和头像包 | 仅白名单；observer 的完整面板解释未导出 |
| `current_observation` | 当前视觉投影；`diagnostics.py:286–295` | 时间、新鲜度、装备形状、页面/地点/任务状态；缺“当时面板实际呈现” |
| `last_query` | 持久化 `tool_audit` 的最后一条，`__init__.py:1509–1528` | 审计可能早于本次提问；规划失败/提前返回可能没有新的审计 |
| `last_query.stages` | 审计中的 refresh，`diagnostics.py:198–266` | 仅最后 48 项，阶段/原因有白名单，其他值变 unknown |
| `explicit_followup` | `_followup_status`，`diagnostics.py:306–319` | 主要状态、reason、ready；当前复制投影缺补答请求 ID、重试细节和底层错误类别 |
| `page_control_trace`、`revocation_trace` | 两个 owner 内存列表，分别白名单输出 | 已有控件/撤销线索；仍不能替代具体拒绝分支 |
| `vision_trace`、`frame_comparisons` | owner 与 vision adapter 的内存事件及像素比较计数 | 合并后最后 48 条，容易被后续 full 覆盖；部分新阶段被导出白名单抹成 unknown |
| `answer_deliveries`、`equipment_handoffs` | owner 两个内存回执列表，`diagnostics.py:349–354` | 各取最近 8 条；插件返回/SDK 提交回执，不是宿主播报确认 |
| `speech` | 仲裁器 snapshot | 待发数/提交数，不证明本题说对 |
| `observation_health` | `recognition_metrics.observation_health:121–141` | 巡逻、8 遍 full 账本、让位/30 秒/350 毫秒计数、身份及清零、当前/提交年龄 |
| `query_timings` | `timing_detail.snapshot:199–207`；入口 `__init__.py:3196` 后追加 | 单个最近请求，256 事件上限；当前不在 copy_text 内 |
| `delivery_value_trace`（显式选项） | `__init__.py:3197–3199`，service_contract 中的显式短期追踪 | F 不自动打开、不并入无隐私诊断，避免引入正文/数值内容 |

### 1.2 为什么看起来“两份”，复制又不完整

`ui/panel_v09.tsx:490–500` 同时显示 observation_health 的独立 Textarea 和“完整报告”中的 copy_text；复制按钮在 `:269–274` 只复制 `diagnostic.copy_text`。

需要纠正一个容易误判的点：E2 的 `diagnostics.py:356–360` 已经把 observation_health 放进 copy_text，所以这块账本并非必然漏复制；两个框是重复展示同一块数据。

真正的缺口在 `__init__.py:3196–3199`：copy_text 在 build_diagnostic_report 内已序列化，query_timings 和可选 delivery_value_trace 随后才追加到外层 report。于是 RPC 返回、面板可见块、复制/文件导出不是完全相同的一份数据。`diagnostic_export.py:11–31` 也只保存 copy_text，不会补后追加字段。

F 应删除这条分叉：先收集全部允许字段，形成一次快照，再生成唯一复制文本。面板和导出引用这份快照，不在序列化后加诊断字段。

### 1.3 跨重启现状

| 数据 | 重启/重新创建插件实例之后 |
|---|---|
| `_recognition_metrics`：最近 8 遍、让位、30 秒/350 毫秒 | 内存清空；`recognition_metrics.py:72–107` |
| `_patrol_status`、anchor、稳定帧计数/年龄补扫累计 | 内存清空；`patrol.py:45–69` |
| engine 身份计数、70 秒起点、清零列表（12 条） | 重建 runtime 后清空；`engine.py:56–59,284–350` |
| `_vision_events`（owner/adapter 各 48 条） | 内存清空；`vision_trace.py:59–64,109–120` |
| 最近 query_timings、活动 span、256 条事件及 dropped | 内存清空；`timing_detail.py:54–76,86–117,199–207` |
| 页控件/撤销记录、当前补答及发言回执、复用缓存、当前内存提交 | 内存重建；不能从磁盘历史恢复“当前可信”资格 |
| prepare 记录 | 失败可进 `prepare_failure_records_v1`，最近 4 条；写失败或忙会缺失，`prepare_trace.py:300–329,383–395` |
| tool_audit、历史视觉/状态、用户主动导出的诊断文件 | 有存储路径可保留；历史不是当前会话证据，不应和新计数混算 |
| 插件/宿主重启总次数 | 当前没有可靠跨进程累计账本，不能从现存运行日志推断全部次数 |

本轮 19:30 两次插件启动使部分计时缺失。F 需显式标会话边界，而不是把“重启前没记录”当作零次。

## 2. 唯一诊断快照与每层内容

### 2.1 公共信封与健康灯

建议 `schema_version=2`，保留现有必要兼容键一轮；新规范以 `floors` 和 `rejections` 为准，兼容键只引用同一采样数据，不能各自再读取一次。

公共字段：`diagnostic_id`、`checked_at_utc`、展示时区、`session_id`、插件实例 ID、来源读取开始/结束时间、`sources_complete`、`incomplete_sources`、每个来源 `source_at/session_id/request_id`、时间对齐状态。每层固定 `health {level, reason, as_of, scope}`、`metrics`、`recent_events`。

红/黄/绿只用于诊断，不进入业务条件：

- 红：所选本次请求已明确失败/过期、确认达到失败终点、采集或诊断来源明确错误；写明哪个请求失败。
- 黄：处理中、部分字段扣留、来源缺失/记录覆盖、信息只到 SDK 提交、旧会话或历史记录。没有测试样本不判绿。
- 绿：该层要求的相关检查确有成功证据，且时间/会话归属明确。单层绿不能代表角色已回答。
- 观察主动关闭标黄“未运行/未检查”，不是故障红，也不是识别成功绿。
- 同时展示 `latest_request_health` 与 `current_runtime_health`，避免一次后来的成功擦掉前一失败。旧 last_query 必须标历史，不冒充刚才的请求。

### 2.2 5F：玩家请求与交付

关键数字/状态：

1. 最近 8 个工具请求的 request_id、入口、开始/结束时间、总耗时、阶段；参数只保留结构化枚举（intent、screen、fields、read_path、source、subject_state、detail、字段是否缺失/空数组）。不存 query、角色名、目标猫娘名、玩家档案内容。
2. 规划 outcome/reason（包括 screen_intent_conflict）、原始选参结构与归一计划结构差异；被拒绝时最后完成阶段、异常类和白名单错误码。未开始 OCR 单独说明。
3. pending_reply 路径：本次工具 request_id、引用的旧待办 request_id、relation、accepted/rejected reason、是否提前返回、是否继续执行本次读屏。记录 actual，不自动修入口。
4. 补答：原待办 ID、generation、原截止时刻/剩余时间、读取/评价/正文/SDK 提交四种完成度；ready 的定义、ready→cancelled 的状态变更；activity 的每次读取耗时、attempt、available/unsupported/unavailable、inflight 占用、超时层级、异常类/安全错误码。保留 API 操作枚举，不保存 SDK 返回体/异常正文/聊天摘要。
5. 消息回执：选中快照 ID、答复状态、缺项、正文投影 mode/required/carried/missing sections、SDK submitted；区分“返回给宿主”和“猫娘开口不可观测”。不采集说话文本。
6. 面板见证：服务端面板 context 的生成时间、observer state、视觉状态、新鲜度、identity_confirmation state/reason；前端最近一次实际渲染的 statusKey、tone、context 序号、rendered_at、UI mount_id。记录 statusKey/原因枚举，不复制 observer.message。

面板“当时”不能用诊断时重算冒充：前端在原 statusKey 分支得出值后只留最近 16 次变化，本地采样；点击一键诊断时经原 action 的白名单参数一并传入，服务端标 `source=client_render_witness`。不额外轮询、不每帧发 RPC；没有前端见证的 API 诊断显示 `not_observed`。仅表示客户端已渲染，不证明玩家确实看过，也不作为任何读屏权限。

最近事件默认 6 条：规划失败、新工具转旧待办、补答登记/就绪/取消/提交、面板状态变化。历史故障按请求保留，不被 idle 陪聊冲掉。

### 2.3 4F：观察与拒绝记录本

保留 observation_health 全部白名单投影，并新增缺失的关联字段：

- 当前 patrol decision/reason/fallback，checks/suppressed/age_refreshes；anchor 是否存在/已受挑战，下一次年龄补扫距离、采用的 lead、真实帧/提交年龄；按 fallback 原因累计，而非只展示最后一次。
- 最近 8 遍 full 的 job_id、输入帧/核验帧 ID、processing_seconds、生命周期最终状态 parsed/verified/committed/yielded/rejected/draining；不可只更新同一行导致 parsed 被覆盖后无法追因。
- 让位次数和所在检查点、30 秒闸门拒绝次数/依据、350 毫秒 page-only 次数，全部带 session 范围。
- engine 身份 required/tracked/pending/confirmed/missing、确认开始/elapsed/limit=70、failed reason、重试次数；清零事件中的 cause、清零槽位/已确认数量。计数确认与本题新帧交付许可分开。
- 当前 raw commit 的 observed_at/age、serial/epoch；最近 parsed/verified/committed 的时间和 job；三者间隔、未提交原因。当前视觉投影年龄/缺项/控制待核验与原始提交年龄分别保留。
- 队列/当前消费者：query、delivery、manual、producer，活跃请求、工作者 job、核验排队状态；只读既有状态，不加调度。
- 拒绝记录本见第 3 节；以链路摘要显示“最后成功点→第一处拒绝→后续处理”。

最近事件默认 8 条，优先拒绝/扣留/让位/提交/清零，成功心跳不刷屏。原 health 保留一处，不再单独做第二块可复制 JSON。

### 2.4 3F：识别与区域计划

- 页面候选与是否 supported/verified、控件输入数/证明数、scene_only/region/full 范围。
- 紧凑裁剪尺寸、源尺寸、裁剪数量、原生文字框数量、姓名候选数、复核数/拒绝数、唯一身份计数、未识别的具体阶段。
- 名字门槛只记录实际检查结果（canonical/单行/置信度达标/重复一致/唯一性），不记录姓名、原始标签、目录实体 ID。
- 各检测器耗时、fact/stat/main/sub 数量，name verification 与 parsing 分开。region_plan 的 source（当前几何/缓存几何/缺失）、page enum、fields、fact_kinds；region_plan_missing 按请求计数，不能单凭出现它就判本题必失败。
- OCR 行映射被丢弃的几何原因、证明溢出数、读取失败/不支持/未执行明确区分；本层解析成功不能盖章可信提交。

最近事件默认 5 条：page 判定、姓名复核、事实提取、区域计划缺失。底层 OCR 耗时在 2F 以 span 关联，不重复求和。

### 2.5 2F：OCR 与资源排队

- 每次 native call 的 operation、job/span、start/end、duration、调用尺寸、返回框数、错误类；不留文本或图片。
- scheduler_wait/native_lock_wait、producer 让位信号到完成、drain、native_work 各自耗时；区分“排队”和“推理”，总耗时不得把嵌套 span 相加。
- 提问到达时已在运行的原生块：adopted=true、开始相对时间可负、总时长和问到后的剩余占用；最近最大慢块和所属 role。
- cooperative checkpoint 核验：入队/执行/完成、请求是否过期、proof 需求数/实际数/确认数、执行帧；仍在跑但等待者超时，与后端真正退出分开。
- backend setup/热身、可读到的后端类型/初始化状态；CPU/内存若没有既有采样就标 not_recorded，F 不增加高频性能巡逻。

最近事件默认 5 条，另保留最近 3 个最慢原生块（同一采样窗口）。复用已有计时，不加第二个 OCR、不卡锁测量。

### 2.6 1F：构建与生命周期

- plugin_version/build_revision/build_commit、安装清单提交标识（存在才展示）、实际 Python 包路径的逻辑来源 installed/source（不导出用户绝对路径）、助手 service_version；标明构建字段缺失/不一致。
- 宿主 PID、插件工作进程 PID、助手 PID（SDK/健康接口能拿到才填），plugin_instance_id/session_id、插件启动记录时间、进程真实启动时间和来源。拿不到 OS birth time 时不得把诊断时间当启动时间。
- 当前会话 uptime；同进程插件实例重建次数，与跨进程重启次数分开。后者只有持久化后才能统计，标明 since/coverage_unknown，首次启用不能声称掌握历史总数。
- 助手 connected/capture state/isolated_window、游戏 running/minimized、最近帧 seq/真实年龄、来源读取失败。F 不操作助手/宿主进程。

现状特别说明：源码 `runtime/build_info.py:3–5` 仍是 0.9.7/旧 revision/unknown，试运行安装时注入了 E2 构建标识，因此源码常量、安装 stamp、面板版本要分别核对，不能因源码常量旧就认定运行错版；本轮不修版本显示。

## 3. 拒绝记录本（核心）

### 3.1 记录格式、存放与安全要求

每条事件固定包含：`event_seq`、`at_utc`、`monotonic_offset_ms`、`session_id`、`chain=compact|full|cache|delivery|followup`、`branch_id`、`source_ref`（固定相对文件:行号/符号）、`stage`、`disposition=reject|withhold|discard|defer|fallback|not_applicable|allow`、`reason`。

关联字段：`request_id`（没有就 null）、独立 `attempt_id`、`job_id`、`frame_seq`/`verification_frame_seq`、两帧真实时间、source/current epoch/serial/generation/capture_revision、候选 observed_at/结果年龄、核验帧年龄、原截止剩余 ms。后台 full 尚无请求就用 job_id，不强贴后来问题；新问题接入/手动刷新引用通过关联事件连接。

定位字段：`check_id`、实际执行的布尔/枚举值、required/present/confirmed/withheld 数量或字段名、`region_index`、`region_role`、量化到 5 位的归一化 bounds、control role/index、入选/未入选/矛盾/缺失。名字与数值用 equality/quality/identity_count 表达，不记真实值或文本散列。截图、OCR 行、raw_label、proof_key 中的文字、对话正文/摘要、UID、玩家库、路径、密钥、Cookie、代理地址一律不进入事件。

复合条件按实际短路执行过程记录 `failed_check`；未执行的谓词记 `not_evaluated`。不能为了记诊断重跑 checked_receipt、matches、OCR 或像素对比，也不能把原 `or` 全部改成预先求值。已有中间结果顺手记录；需要展开复合条件时保持顺序、异常与调用次数等价，用行为对照测试约束。

建议独立侧栏 `runtime/rejection_trace.py`，归 trace，事件只接收原分支已经知道的标量，不能读取业务对象推理“为什么”。装配入口持有 recorder，低层只追加。保留原 vision_trace/timing 的职责，新本子替代分散临时拒绝记录，不再另加第二套权限判断。

容量建议：

- 全局拒绝环 256 条；不同请求保留最近 8 份摘要，每份最多 32 条关键事件；后台最近 8 个 job 每份最多 16 条。都是固定上限，不是把 48 改大就算修复。
- 细节环可覆盖最老事件，但每请求/每 job 的“最后成功点、首次拒绝、末次拒绝、终态及原因累计”在其摘要槽被淘汰前保留；记录覆盖数、丢失范围、记账错误数。
- 同一分支同一依赖连续重复合并为 first_at/last_at/count；不同 request/job/frame/区域不能合并。成功记录只留阶段迁移/最终许可，不记录每次轮询。
- 异步/线程同时写入时仅短临界区追加/复制；序列化和落盘不在 OCR 锁内。记录器不创建识别任务、不等待、不抛入产品路径；`CancelledError`、原异常传播保持原样。关闭记录器或注入故障应得到同样业务结果。
- 一次请求的终态必须可见；若其失败原因为旧 full 引起，按关联 ID 找到对应 job 的拒绝，不从最后一条全局事件猜测。

### 3.2 快读、姓名与区域核验分支清单

表中位置是 E2 基线；“字段”均叠加 3.1 的公共关联信息。表内用分号列出的谓词/出口在实现时逐项登记独立 check_id，不能继续共用模糊的 unknown。

| ID | 位置 | 现有拒绝、扣留或回退点 | 要追加的具体证据 |
|---|---|---|---|
| Q01 | `current_regions.py:60–69` | 字段不适用；reader 不存在 | fields、reader_available，记 not_applicable 而非 OCR 失败 |
| Q02 | `current_regions.py:79–94` | 拿不到源帧；_live 失效；返回非 dict；不是角色页；scene 未证实；deadline 已到 | capture 状态/age；enabled/stop/generation/epoch/failure 分项；analysis/page 类型、elapsed |
| Q03 | `vision_scheduler.py:183–217` | 排队后已取消/过期；prepare 空；prepare 后失效；原生异常/退出仍在 drain | queue/drain/native 时长、deadline_remaining、cancel cause、实际 worker 状态 |
| Q04 | `character_regions.py:45–104` | 单裁剪/图集过大；框键缺失/非数值/非有限/空几何；框跨裁剪或落在所有裁剪外 | crop/index/尺寸、框输入/映射/丢弃数和原因；不记录框文字 |
| Q05 | `character_regions.py:107–120` | 图像超限/解码错误；preview 空/页型不符/scene 未核验/无控件 | source_size、atlas_size、boxes_count、page enum、control_count、error_type |
| Q06 | `text_verification.py:160–173` | 姓名候选不唯一、置信度不足、规范名不完整、几何无效，导致没有待复核候选 | 候选数量及每项 eligibility flags；零候选不是“复核通过” |
| Q07 | `text_verification.py:177–222` | 只处理前 32；scale<1 跳过；无 batch；复核异常吞下继续；取消向上抛 | selected/skipped/overflow、scale、native operation/框数、异常类、cancelled |
| Q08 | `text_verification.py:223–249` | 复核姓名不匹配/低置信/不是完整名/额外非字段文字/name_lines 非唯一 | 分项 flags、name_lines_count、fields_valid、confirmed/rejected；不导出原名 |
| Q09 | `character_regions.py:121–127`; `contracts.py:216–225` | 不是 detail_subject；不满足 native_name_confirmed；多/零实体将 actors 清空 | actors before/after、scope/method/equality/score flags、distinct_count；维持原门槛 |
| Q10 | `current_regions.py:95–108` | 无 controls；后帧缺失/窗口 PID 不同/生命周期失效 | control_count、两帧 ID/age、pid_same、lifecycle 分项 |
| Q11 | `current_regions.py:109–119` | 控件像素变化，整包返回空 | source/validation frame、每个 changed control index/bounds、比较方式/耗时；不得只报 controls_changed |
| Q12 | `current_regions.py:120–127` | preview 发布后代次不在允许范围；_live 不再成立 | epoch before/after、publication_id、哪个 life guard 失败 |
| Q13 | `current_regions.py:132–156` | guard 生成异常；review 前超时/失效；两次 review 均 superseded | proofs 数/区域角色；retry index、source/current serial、review result 分类 |
| Q14 | `current_regions.py:157–161` | result None；_SUPERSEDED；_live false；checked_receipt false；snapshot.matches false | 严格短路顺序下首个失败子项，并关联 P01–P05/B01–B06 的下层事件 |
| Q15 | `current_regions.py:30–44` | 可选 page-only guard 不建立 | 已有 guard/无 preview/证明覆盖不齐；记未建立，不误称整个身份包被拒 |
| Q16 | `region_query.py:56–85` | 无 fields/reader/commit；region_plan_missing；空区域 | plan source、page、fields、fact_kinds/count、region_count；区分主快读失败后的 fallback |
| Q17 | `region_query.py:91–138` | deadline/no_frame；superseded 重试后 busy；空框/无 best frame | attempt、frame age、boxes_count、best packet 是否保留、stop reason |
| Q18 | `region_query.py:123–134,149–184` | 队伍名单短/未知、槽位不完整导致部分结果 | expected/read counts、slot state 计数；不记录角色名单、不把部分当全 |

### 3.3 文字证明、缓存与快照核验分支清单

| ID | 位置 | 现有检查与出口 | 要追加的证据 |
|---|---|---|---|
| V01 | `text_verification.py:58–91` | >16 条未安排；无效/隐藏/过长预期；坏坐标；空几何；crop 超限 | proof index/role、input/selected/overflow count、skip reason；不拷贝 trace 中 expected/actual |
| V02 | `text_verification.py:96–143` | 质量不足；多行；数值语法不合法；高置信矛盾；不足两次 exact match | scale、valid/quality/numeric flags、matches、contradicted、框数；不调阈值 |
| V03 | `delivery_scope.py:45–81` | identity revision 不符/not admitted；原身份证明缺失或歧义 | serial/epoch、admitted flag、proof match_count、identity role/index |
| V04 | `delivery_scope.py:84–135` | 无 page controls；事实无 literal/几何；身份不足；guard coverage 不全 | required/present coordinates、缺失 control/proof 的 index/role；是否只扣身份 |
| V05 | `delivery_scope.py:139–160` | 整包 scoped_guard 失败后，字段分别尝试仍无结果/部分结果 | 每个 field 的独立结果、admitted/missing fields；禁止只留末次探测理由 |
| V06 | `query_reuse.py:272–358` | producer receipt 不可用；无可答候选；旧候选降级后的重选/两次 superseded | candidate slot=current/previous、核验前后 coverage、选择/拒绝原因、attempt |
| V07 | `query_reuse.py:380–418` | 缺候选、已 invalidated；observer 不适用；无 scope；坏时间/epoch；超过60秒 | checked predicate、observed_at/age、fresh/stale policy、source/current epoch |
| V08 | `query_reuse.py:419–452` | stale_not_allowed；无 client/PID；取帧后版本变化；捕获不健康/新帧超2秒；坏图 | client_present、pid_known/same、capture flags、fresh-frame age、serial/generation 对照 |
| V09 | `query_reuse.py:459–485` | pixel_only 不能 native review；producer busy 无协作入口；文字核验后被 superseded | global/text changed、cooperative available、role、verified coordinates 数 |
| V10 | `query_reuse.py:486–522` | 换核验帧后版本/patch count 不符；OCR 后帧变旧、更新帧不可用/不新鲜/不同 PID；新帧证明失效 | 两次 frame IDs/age、patch counts、retained proofs、哪个更新条件失败 |
| V11 | `query_reuse.py:539–563` | 保留旧包 proof rejected；证明不足，defer；状态 timeout/busy/error 等不允许 partial | owns_current、allow_partial、same_text.state、deferred coordinates、是否 retain job |
| V12 | `query_reuse.py:565–593` | 无足够控件；pruned scoped_guard 空；仍引用未证实区域；复查期间 superseded/patch变化 | proved/unproved controls index、partial fields、二次 patch/result |
| V13 | `query_reuse.py:595–620` | 全局/文字仍变化；最后 epoch/serial/enabled/stop/pending 改变 | 每个变化标志、control quarantine exception 是否适用、withheld scope |
| V14 | `live_refresh.py:959–1018` | 工作繁忙；demand 过期；无 reader/current；证明集合不覆盖；旧 job 换依赖需排空 | demand origin/deadline、queued/running、dependency_diff_keys、missing coordinates |
| V15 | `live_refresh.py:1020–1108` | 另一 demand 已占 job；旧 worker 未退出；检查点失效；frame不可用；guard/literal覆盖缺失；enqueue不可用 | owner request IDs、queue/execution frame、coverage counts、error code、cooperative 状态 |
| V16 | `live_refresh.py:1111–1168` | 收到时过期/无帧/版本变；expected proofs不匹配；2秒等待切片忙；superseded/cancelled/error | 同一 job 的 retained flag、confirmed coords/count、short-circuit cause、error_type；busy 不误报文字矛盾 |
| P01 | `review_policy.py:62–77` | checked_receipt：不可观察/answer_generation不同；fields未覆盖；validated_at坏/超2秒；matches失败 | 分开记录每个 guard，关联 boundary；不是只记 receipt_false |
| P02 | `review_policy.py:80–107` | review_current：不可观察/有 preview/证据不 fresh/原 guard 缺区域/无 pairs | preview/commit presence、required/present region indices、freshness state |
| P03 | `review_policy.py:115–122` | review_current 返回 superseded/None | 关联 review_snapshot 的事件 ID，不覆盖其具体原因 |
| P04 | `review_policy.py:135–160` | producer receipt：无 plan/不可观察/preview/generation；证据/serial/epoch/failure/query/answer不符；检查帧不新；pending；无scope/deferred相交 | 每个实际执行 guard 与 expected/actual 编号、frame_age、pending类别、交集区域 |
| P05 | `review_policy.py:169–170` | producer receipt 最后 snapshot.matches 拒绝 | boundary result、字段扣留、source/current revisions |
| B01 | `delivery_boundary.py:83–98` | epoch_changed/new_question/capture_failed/observer_unavailable/source_unconfirmed/pending_unproved | 每种原因独立 branch_id；invalidated 与 epoch mismatch 分开注明 |
| B02 | `delivery_boundary.py:100–134` | page_changed/revision_unproved/unscoped_revision/page_proof_missing/page_control_challenged | page enum、source/current serial、required/admitted/reviewed/challenged 控件 index |
| B03 | `delivery_boundary.py:136–188` | challenged withholding；装备更换/事实不在当前包；visible_text不一致 | withheld field/区域、object_same bool、equality counts；不记录装备内容 |
| B04 | `delivery_boundary.py:190–216` | 角色不在当前包且无同版身份许可；操控角色变化；名单/数值字段缩水 | identity proof/revision flags、actors before/after、fields withheld |
| B05 | `delivery_boundary.py:219–229` | partial 与 retained 最终区别 | reason、withheld_fields、可交付字段；扣留不是全局撤销 |
| B06 | `observation_store.py:119–125` | matches 因 attributes 失去 owner 或所需字段被扣留而 false | requested_fields、boundary_state、withheld_fields、owner_changed；复用该次 delivery 结果，不再调用一次 |
| D01 | `live_refresh.py:919–958` | 兼容 validate：已失效/停用；版本变；无后帧/捕获不健康；controls变化 | manual/offline/live 路径、当前帧 age/PID、revision；保持原离线例外 |
| D02 | `live_refresh.py:1203–1248` | validated_delivery：producer receipt miss；证据不fresh且不能重选；无助手旧样本；不可观察；旧receipt；serial/epoch变/validate失败 | chosen path、reselection eligibility flags、rejected receipt/boundary event IDs |
| D03 | `live_refresh.py:1266–1357,1430–1530` | observer不可用；初始reuse超时；page-only补充超350ms；原期限结束/取消；stale fallback仍无结果 | original deadline、fresh/stale reserve、completed/partial、350ms计数、最后拒绝ID |

### 3.4 full 解析、控件复核与提交分支清单

| ID | 位置 | 现有检查与出口 | 要追加的证据 |
|---|---|---|---|
| F01 | `live_refresh.py:377–403` | consumer未放行、foreground等待，full尚未开始；先前guard变化触发defer/revoke | phase=not_started、role、active demand、是否实际启动OCR；不要算作已丢一遍full |
| F02 | `live_refresh.py:482–492,623–634` | 检查点 QueryPreempted/ObservationSuperseded、worker异常 | checkpoint、native block id、yield request/result、原生是否已退出 |
| F03 | `adapters/native_text.py:105–239,358–385`; `adapters/local_vision.py:369–445,610–662` | 格式/尺寸不支持；逐块OCR错误；读取预算退出/区域未扫完；name/portrait让位；顶层异常转失败包 | tiles total/completed/skipped、exit enum、elapsed、checkpoint耗时、fact/character counts；禁止异常原文 |
| F04 | `live_refresh.py:538–555` | 分析中capture不可用；无controls路径判page_changed_during_analysis | health flags、frame IDs、native_page_guard、coarse compare；不推测真实切页 |
| F05 | `live_refresh.py:564–608`; `page_handoff.py:59–193` | 探针失败/未知/旧页/过时/达到上限；真正handoff停止旧任务 | probe attempt/native_seconds、result、request_current、controls/baseline_updated；沿用现有原因枚举 |
| F06 | `live_refresh.py:611–621` | stopped worker未在原drain时限退出 | draining job/span、pending count、duration、backend_busy；不启动替代worker |
| F07 | `live_refresh.py:635–665` | parsed后changed/epoch变；post_ocr让位 | parsed counts、source/current epoch、yield checkpoint、postparse→stop耗时 |
| F08 | `live_refresh.py:668–711` | 解析后新帧不健康；literal_review让位/superseded；核验后epoch变 | validation frame age/PID、proof stage、cancel cause、原frame/新frame |
| F09 | `live_refresh.py:158–275` | stable_screen_content移除identity、facts、visible rows；未获足够page controls；证明只处理前16 | 每项 required proofs/confirmed/pixel stable、丢弃role/index/field、page_controls_revalidated；替换含raw文字的临时trace输出为安全投影，不移除原返回规则 |
| F10 | `live_refresh.py:724–765` | page_controls未复核；native patch变化；最终changed或epoch变整轮丢弃 | coarse/native两项、changed region indices/bounds、input/proved controls、scope、epoch |
| F11 | `live_refresh.py:788–812` | 身份guard不成立；delivery/full guard区域>48、行/事实无完整几何 | guard type、input/covered/overflow、all_rows/facts_guarded、identity_guarded；候选可存在但不可用于某种消费 |
| F12 | `__init__.py:1298–1329` | guarded_analysis空则继续；提交前助手/窗口健康不符 | 上游终态关联、helper/window flags、PID same、此时不发生commit |
| F13 | `__init__.py:1334–1343`; `fact_lifecycle.py:64–81` | 总耗时>30且late_read_proved不成立 | elapsed、candidate validation完整性/epoch/PID、30s计数；只记录原闸门结果 |
| F14 | `__init__.py:737–750` | 等 corrections 后已撤销/stop；yielded incomplete 保留旧包 | epoch/stop、recognition/portrait yield flags、commit not_started理由 |
| F15 | `__init__.py:751–791` | 角色不在目录/类型不对/规范名不符，过滤候选；纠正匹配分支 | before/after数量、catalog resolution/equality flags、correction applied bool，不留名字/指纹 |
| F16 | `__init__.py:792–803`; `fact_lifecycle.py:83–114` | same_page_incomplete_read保留旧提交 | page_same、input/current coverage、field_changes bool、retained_serial、identity issue |
| F17 | `__init__.py:831–868`; `engine.py:104–235,284–350` | 场景未稳定而当前state空；身份未攒够/slot_missing/replaced/空名单等清零；70秒失败 | 同帧 scene/identity前后计数、required、清零cause、confirmation elapsed/state；不是把未确认身份当已丢全文 |
| F18 | `__init__.py:869–920` | 序号/磁盘写入后又epoch变或stop；仅store有写而内存commit没完成 | store_started/finished/error、target serial、revoked phase、commit_called/finished；不把写last_local_vision当可用提交 |
| F19 | `observation_store.py:190–245` | commit取消旧guard job、更换内存结果/历史保留；异常中止 | previous/new serial、raw commit完成、history_only标志、job cancellation cause；失败记录后原异常照常传播 |
| F20 | `__init__.py:945–975,1380–1388` | full范围不满足所以last_full_serial不前进；producer_validation/guards不齐；patrol anchor未建立 | raw serial、last_full_serial前后、read_scope、guard counts、receipt availability、anchor reason |
| F21 | `observation_store.py:165–188,249–270`; `fact_lifecycle.py:133–end` | 已提交字段在当前投影被pending/deferred/freshness遮住 | raw_fields/projected_fields、pending类别、challenged regions、current/raw ages；区分“没有commit”和“commit不可用” |
| F22 | `patrol.py:72–130`; `live_refresh.py:845–907` | 巡逻fallback/稳定skip/年龄补扫；帧监视defer已提交区域 | decision/reason、anchor/age/next_refresh、changed coords、defer/clear cause；不改调度 |

### 3.4.1 页面探针与原生准入的细项（归入 F05/Q03，不另做判断）

| ID | 位置 | 逐个要区分的原分支 | 记录字段 |
|---|---|---|---|
| H01 | `page_handoff.py:13–47,59–76` | observer/请求期限无效；reader/enqueue缺失；控件或expected页型无效；间隔未到；次数上限 | demand来源/remaining、reader/submit flags、controls_count、page enum、attempts/next_at；not_scheduled原因 |
| H02 | `page_handoff.py:77–97,116–143` | 同一frame提前去重；任务obsolete；capture不健康；取帧后obsolete；再次同frame；控件未变化 | request/revision对照、frame序号、capture flags/age、baseline token/controls index |
| H03 | `page_handoff.py:144–163` | probe原生调用取消或异常；执行后过期；候选未核验/页型unknown | native耗时、异常类、obsolete flags、candidate verified/page；不吞掉原因 |
| H04 | `page_handoff.py:164–181` | 无proved controls；后帧不可用；请求过时；控件覆盖不足或像素变化；确认仍同页 | input/proved/patch counts、changed control indices、frame age、baseline_updated；同页是未交接而非识别失败 |
| H05 | `page_handoff.py:186–193`; `live_refresh.py:582–608` | enqueue返回None；probe future异常；confirmed_transition但result_current不符 | queued/result状态、obsolete具体依赖、old/new page枚举；不丢弃最初probe原因 |
| H06 | `vision_scheduler.py:69–71,126–150` | vision_closing/drain超时仍有原生任务；拿锁后再次blocked；drain失败 | role、lock wait、retained task数量、blocker flags、drain结果/耗时 |
| H07 | `adapters/local_vision.py:25–49` | 原生串行锁0.2秒未取到；cooperative callback检查取消/让位 | native_lock_wait、cancel/yield原因、call是否实际开始；保留原异常类型 |
| H08 | `adapters/native_text.py:20–22,130–144,390–410` | check_current取消；yield_requested；区域参数解析失败/空或无效区域 | checkpoint、cancel cause、region role/index/bounds、native call started；不记录payload |

`F09` 的每项移除还要记录 `no_geometry`、`patch_missing`、`pixels_changed_not_revalidated`、`proof_outside_16_limit`、`proof_read_error` 等实际依据，不能只写 `facts_removed=N`。这些是诊断细分，不改变原 `stable()` 返回值。`F21` 的字段可用性遮罩也要记录前后数量和现有 unavailable/freshness reason，避免把“没有被选中”混成“从未生成”。

### 3.5 工具与补答最后许可（排除“读到了但没说”）

| ID | 位置 | 必记事件 |
|---|---|---|
| X01 | `__init__.py:2414–2419`; `pending_handler.py:10–60` | 新调用携带pending_reply而提前返回；relation/旧request校验结果；当前读屏实际未执行 |
| X02 | `__init__.py:2442–2444,2817–2837`; `query_plan.py` | planning成功/冲突/异常及白名单原因；不依赖成功tool_audit才记录 |
| X03 | `__init__.py:2586–2631,2807–2814` | 最后snapshot delivery扣留/身份失败、交付前快照复查；插件payload回执，非宿主消费 |
| X04 | `followup_delivery.py:245–280,310–354,435–450` | 不登记/可继续/到期/取消、页面或对象变化；ready及service完成度转换 |
| X05 | `chat_activity.py:96–154`; `followup_delivery.py:143–225,451–466` | unsupported/inflight busy/调用超时/Err/异常、重试预算耗尽；activity ready前后关联；不再只有unavailable |
| X06 | `followup_delivery.py:502–539,570–606` | empty_projection、final_guard、new_question/expiry/discussion/epoch/snapshot变化、silent/SDK提交失败或成功；保留具体首个检查 |

原生函数、解析器和入口的任一未预料异常也要落固定 `unhandled_stage_error` 终态（异常类+白名单code），随后原样传播/按既有规则转换，不能改为吞掉异常。`return None` 并不一律叫失败，not_applicable/defer/retain/partial 各有明确定义。

## 4. 一键复制全部与长度控制

建议保留一个“复制全部诊断”按钮。普通界面为 5 个摘要行+每层详情，不再同时出现 observation_health 小框与另一份不完整大框。复制失败显示同一份文本供手动选取。

复制文本格式：UTF-8 纯文本，开头固定 5 行摘要（5F→1F，每行颜色字样、当前请求ID短号和核心数字），随后一个 `schema_version=2` 的完整 JSON。若保留 `.json` 文件下载，文件内容为同一规范快照的 JSON，复制开头摘要由该快照渲染；两者 diagnostic_id/source_seq/内容指纹一致，不能复制时另发一次查询。

例如：

> 5F 红｜请求…｜7.48s 未确认｜补答 ready 后活动读取失败取消
>
> 4F 黄｜解析有进展，可用提交未前进｜首拒 Q14/receipt_generation
>
> 3F 黄｜姓名候选1，许可未完成｜区域计划回退缺失1
>
> 2F 黄｜排队1.22s，native0.78s｜无记录不写0
>
> 1F 绿｜E2/596385d｜本会话…｜历史重启次数未知

以上仅为格式示意，其中拒绝细项是未来采样结果，不能当作本轮Q14实际已定因。

实现时需同步调整 `diagnostic_export.py:12–16` 当前“对 copy_text 做 json.loads”的假设：JSON 文件从同一规范快照对象序列化；复制文本由同一对象生成摘要前缀与JSON。不能把带摘要的文本直接当JSON文件保存。schema v2明确这一区别，旧顶层兼容键从同一快照投影；测试和本地分析工具按schema_version解析。可选 delivery_value_trace 保留独立显式入口，不混入默认安全复制。

拒绝本的完整有界内容在快照顶层只存一次；4F只放关联索引、统计和最近事件视图，避免把同一256条环再复制五遍。snapshot_token使用诊断局部编号，不对角色/词条/文本计算可逆查表的内容哈希；已有内容digest只按既有脱敏导出策略保留，不新增聊天散列。

长度建议：复制/文件统一保留现有 128 KiB 上限，目标不超过 96 KiB；按 UTF-8 字节而非字符计数。首先限制单事件字段/每层recent数量；再删除重复成功细节和最老非关键事件。必须保留5层摘要、所选请求终态/第一处拒绝/关键区域、对应full首次停滞、来源时刻和会话、全部截断/丢弃计数。

单事件建议最大 1 KiB，region列表单事件最多8项，其他区域记录独立有界事件+overflow数。若极端输入连必保留部分都装不下，返回明确 `diagnostic_incomplete/size_limited`，不能截断JSON或宣称“全部记录已复制”。“复制全部”指这份有界快照全部，不是无限历史。

序列化前一次白名单清洗：新增状态/原因需配套 schema 测试；未知值记 `unmapped_code`及计数/来源固定check_id，不能 silently unknown 而毫无定位线索。绝不以自由字符串或repr兜底泄漏数据。

## 5. 跨重启选项

| 选项 | 收益 | 代价与限制 |
|---|---|---|
| A：只留内存，玩家主动导出 | 最简单、无后台磁盘写入 | 崩溃/重载丢关键现场；重启次数仍未知；与本轮痛点不符 |
| B：本机有界安全账本（推荐） | 保留上一会话拒绝与终态，能解释重载前后；不上传 | 需白名单、轮转、损坏恢复、隐私和异步写失败测试 |
| C：完整诊断全文自动持久化 | 排障范围大 | 大量重复、隐私面更广、I/O复杂；本轮不采用 |

建议 B：仅事件安全投影和会话头，默认实现前由用户确认配置取舍；本方案不实际启用。若采用，保留当前和最近2个已结束会话、最长7天、总量上限8 MiB（先到先止）。写入使用单个有界队列和批量后台writer；OCR/查询路径只追加内存，绝不等待落盘。队列满时计数、明确持久化缺口，不反压识别。异常退出可能丢尚未flush尾部，报告必须说明，不能保证零丢失。

生命周期记录 session_started/session_ended 或 unexpected_previous_end。跨进程只用UTC及各会话内offset；不得相减不同进程monotonic。restart_count是“启用此账本以来观察到的会话启动数减1”，不是机器历史总重启数；同PID实例重建另计。旧会话只读展示，绝不恢复身份计数、pending_answer、核验回执或可信缓存。

持久化不存聊天原文/摘要/哈希、OCR文字/数值/姓名、截图/图像指纹、账号UID、配置、环境变量、绝对路径、凭据和原始异常。只写本机插件诊断目录，Git忽略；不进入打包清单、不上传遥测。轮转/关闭/清理机制属于将来实现并需单独批准，本轮不删文件。若不采用 B，F 的内存/复制能力仍可先独立实施。

## 6. 改动清单、归属与风险

以下是待批准后的预计修改，不是本轮已改文件。

| 文件 | 真实归属 | 修改内容 |
|---|---|---|
| `runtime/rejection_trace.py`（新） | trace侧栏 | 固定schema、分支记录、有限环/请求和job摘要、fail-open；无业务判断 |
| `runtime/diagnostics.py` | entrypoints（现有） | 5层投影、健康展示摘要、时序来源对齐、统一copy快照 |
| `runtime/diagnostic_reader.py` | trace（现有） | 明确各来源耗时/失败；不发OCR、不恢复观察；补来源版本标记 |
| `runtime/diagnostic_export.py` | trace（现有） | 保存统一快照；兼容JSON导出与长度/不覆盖要求 |
| `runtime/recognition_metrics.py` | trace（现有） | 原账本安全投影、关联ID与计数scope；不参与巡逻/确认判定 |
| `runtime/timing_detail.py`, `vision_trace.py`, `page_control_trace.py`, `revocation_trace.py` | trace侧栏 | 复用span/事件关联，统一导出白名单；不另开性能循环/扩充旧缓冲冒充根治 |
| `runtime/current_regions.py`, `region_query.py`, `query_reuse.py`, `review_policy.py`, `delivery_boundary.py`, `delivery_scope.py`, `observation_store.py`, `fact_lifecycle.py` | L2b | 对应Q/V/P/B分支插入被动事件；复合条件维持短路/调用次数 |
| `runtime/live_refresh.py`, `vision_scheduler.py`, `patrol.py` | L2b | full/排队/让位/年龄/350ms各点关联与终态记录；调度和期限不动 |
| `detectors/character_regions.py`, `adapters/text_verification.py`, 必要的`adapters/local_vision.py` | L2a | 检测/裁剪/姓名/文字核验分支只输出safe flags/计时；不外露原始trace |
| `adapters/native_text.py` | L1 | 已有原生块/退出原因只做安全记录，原循环/预算不变 |
| `runtime/query_plan.py`, `pending_handler.py`, `followup_delivery.py`, `chat_activity.py`, `engine.py`, `service_contract.py` | 分别L3/L5（按现有声明） | 现有决策点记规划/旧待办/活动失败/确认终点/交付回执；不改变返回和重试 |
| `__init__.py` | entrypoints | 装配一份诊断快照、生命周期、panel witness、commit前后与异常终态；不重写业务流程 |
| `ui/panel_v09.tsx`, `i18n/zh-CN.json` | UI（不属Python层） | 单份楼层展示、一键复制、有限客户端渲染见证；现有状态优先级不动 |
| `layers.toml` | 分层配置 | 只登记新trace模块，不改旧归属；baseline保持零 |
| 可选`runtime/diagnostic_journal.py`（新） | trace，writer由入口生命周期驱动 | B选项获准才做；本地安全事件轮转，不让低层读宿主/上层 |
| 诊断/分支覆盖/等价性/UI/隐私测试 | tests | 新增断言，不放宽旧断言；本轮不动旧Node主动搭话Card问题 |

主要风险与处理：

1. **日志改变时序。** 记录只做小标量追加、无新await/同步I/O；昂贵格式化在诊断侧。先用已有重放比较产品trace和耗时；超额就删重复采样，不延长8秒。
2. **拆复合检查改行为。** 对每个本来短路的谓词做调用次数/顺序及异常对照，诊断的错误不得变成产品异常。只消费实际执行值。
3. **把无记录当正常。** 未采到/来源超时/跨会话/被覆盖都显式标黄；保留本次和历史的区别。
4. **复制与显示再分叉。** 一个diagnostic_id、一份规范对象、一处渲染；UI/JSON/手动复制fallback按同ID验证。
5. **隐私外泄。** 白名单数据类型在记录入口和导出处双重约束；不复制底层验证trace原对象，异常只留类型/安全code。
6. **分层回退。** trace只接收事件、不反读上层；装配入口收集跨层数据；既有owner私有访问债不在F顺手重构。

### 推荐实施切片（每片需另行授权）

- F1：统一快照、楼层投影与复制；盘点已有事件映射、来源时刻和会话头，不混入识别修复。
- F2：按第3节补拒绝记录本和终态，将同一请求/生产job串起来；优先Q14及F07–F21，不漏下层predicate。
- F3：安全持久化选项另行确认；完善UI见证/联动测试；获得安装授权后只做最少实机定位。

是否合并F1/F2提交由后续授权决定；本轮没有开分支/提交或新PR。后续PR继续使用本机Git现成登录，不接连接器，不读取/打印/保存凭据。

## 7. 测试计划与最少实机验收

### 7.1 离线必须通过的门槛

1. **分支覆盖表。** 第3节每个branch_id有至少一个命中用例；复合检查分别命中原短路顺序。input缺失/not_applicable和真实reject分开。每份ready/partial/timeout/error/cancelled都有终态，所有事件可关联request或job。
2. **原失败结构回放。** 用本轮保存记录验证Q14未决/控件变化/旧提交持续老化/region_plan_missing/活动失败。仅能证明“现在会记哪个原因”；没有原始分支证据的历史失败仍标未定因，不能反向编造。
3. **业务等价。** 记录器开/关、记录器抛异常、队列满、并发写、多请求替换等条件下，返回字段/许可、姓名确认、15/60/2秒、8秒、70秒、350ms和旧断言全部不变。错误/取消原样传播，无额外OCR或验证调用。
4. **跨路径保护。** 角色A问完立即换B、原完整主词条+4副词条/缺项说明、补答确认/取消/玩家补数、关视觉助手按档案配队；主要用原离线保护用例，不要求玩家再跑整套。
5. **边界容量。** 48旧事件持续刷入不冲掉本题摘要；独立环/请求槽满后dropped/overwritten可见；终态在保留窗口可找；嵌套span不重复算总耗时。
6. **单份复制。** query_timings和observation_health一起进入copy_text/导出；同diagnostic_id。模拟中文/多区域长输入，以UTF8字节达128KiB边界仍是合法完整格式；受限时明确size_limited。保留JSON下载解析兼容。
7. **隐私。** 用伪造token/UID/聊天/名字/OCR文字/异常路径做哨兵输入，内存投影、复制、导出/可选落盘均不出现；有效额外字段也不穿透。
8. **来源对齐。** 诊断生成早于工具完成、last_query仍为上次、插件重建、新旧会话混合、缺UI见证等都准确标示，不沿用旧失败/成功给新问题定性。
9. **持久化（如批准）。** 损坏/截断文件、写权限失败、队列满、跨重启、尾部未flush、上限轮转；旧记录只能诊断，不进入任何身份/缓存权限。
10. **三道门。** 产品实现阶段跑相关定向测试，再在提交前依仓库规矩跑全量pytest（实际通过/跳过/失败数如实报）、Ruff、分层检查；baseline零新增。UI诊断相关测试单列；既有主动搭话Card失败保留S4，不趁机放宽。

耗时对照使用已有保存画面/常驻重放条件，分别给记录器关/开原始样本时间与中位数/范围；纯追加事件微测给最大与p95开销，预设目标单事件p95<0.2ms、单次查询记账累计<10ms（实施前测量核实，不作为业务放宽阈值）。本轮不运行重放、不读取新游戏帧、不宣称性能已达标。

### 7.2 未来安装后的简短实机清单（需另行批准）

只换角色提问2–3次，不重跑遗器/配队全套，不录帧：

1. 打开真珠详情，问一次“这是谁哇”。回答后点一次诊断并复制全部。核对5F→1F摘要齐全、请求ID对应、面板见证时间不晚充作此前状态；成功必须看到准入/提交链，失败必须看到首个拒绝check和具体控件/区域。
2. 换绯英详情，立即问同一句，再复制一次诊断。重点看新frame/object、快读Q14哪个子项，以及full的parsed→verified→commit/projection是否前进；旧真珠结果不能获放行。
3. 仅当前两次没有命中待查拒绝，或存在一个明确诊断缺口，才追加一次换回角色提问。若已定位就结束，不为了凑次数重测。

玩家只记录真实角色、提问/开口时间和复制文本。分析端用request/session/frame/job关联，不要求玩家抄技术参数；若需要补答终态，保持同页等原期限结束后再复制一次，不再提新问题。

F实机通过条件是“失败可精确定位或成功证据贯通”，不是角色识别成功率提高。若仍只有region_receipt/timeout却没有具体check，F自身验收失败；即使猫娘这次答对也不能替代诊断验收。

## 8. 待办与停止点

以下仅登记，本轮不动：

- **首次手动开启陪玩经常失败、重载后才可用。** 用户现象已收；尚未建立首次enable→初始化→采集→首次commit的同request证据，原因未知。后续单独诊断/修复，不擅自自动重载或加等待。
- **面板显示0.9.7。** 源码常量、构建时注入及UI展示字段需在S4统一；本轮只在1F呈现来源和不一致，不修改版本。
- 本轮已知的快读最终许可不明、full解析后可用提交不前进、活动读取失败、旧pending承接新问题等保持“未修”；F只补定位证据。

完成本文后停下等待确认。任何识别/交付修复、E3实施、安装、实机或录帧均需下一轮单独授权。