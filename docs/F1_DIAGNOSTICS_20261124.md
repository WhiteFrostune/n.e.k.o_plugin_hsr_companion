# F1 一份快照、五层诊断与首次启用记录（2026-11-24）

基线：PR #23 / E2 `596385d30363e095c724a362f59adfa1786c799a`。
分支：`codex/0.9.8-f1-diagnostics-20261124`；草稿 PR 的 base 仍为 E2 分支，不是 main。

## 本轮做了什么

F1 仅实现诊断。未修改识别、交付或调度判断，未修改 8 秒、姓名确认、15/60 秒、2 秒新帧、70 秒终点、350 毫秒窗口。F2 拒绝记录本和 F3 持久化均未实现。

1. `schema_version=2`，先收集 query_timings、observation_health、来源采样时间、启用账本及 UI 见证，再构造唯一规范快照，分配一个 diagnostic_id。旧 checks/last_query/observation_health/query_timings 顶层键由同一对象投影，保留一轮。
2. 面板五行摘要、每层详情、复制的完整 JSON 和文件 JSON 都来自该快照。json_text 是同一次序列化的 JSON 表示；copy_text 是五行摘要加同一 JSON。复制不二次查询；导出已有诊断时携带 diagnostic_id，复用最后一份内存快照，找不到该 ID 明确失败，不偷偷生成不同 ID。
3. 红黄绿不参与产品许可。最新请求与当前观察各有 health；旧 last_query 标历史/未匹配，无记录用 null/未记录，观察关闭标黄未运行；当前页面可用但身份仍确认中时，4F 仍标黄。source/time 对齐比较真实时刻，不按不同 UTC 偏移的字符串排序。
4. 客户端只保留最近16次 statusKey 变化；随一键诊断传入固定白名单，记录其 context_seq/session、渲染时间与原因枚举。不新增轮询。没有见证显示 not_observed，不把服务端重算当成玩家当时看见的状态。
5. 默认诊断不含对话原文、摘要、散列、OCR文字、角色名、UID、路径、密钥或凭据。旧 prepare 记录只投影参数形状/路由枚举；原内容散列、角色 ID 和源码异常路径不进入规范快照。未知枚举输出 unmapped_code 并计数。显式 delivery_value_trace 独立于默认复制和缓存。
6. 文本按UTF-8字节计量，目标96KiB，硬上限128KiB。先限制参数结构展开，再收缩可选重复事件，均保留缺失/受限标记；极端输入返回带 size_limited 的合法快照，不切断 JSON。原有整包大小断言保留。
7. 1F 区分有效代码常量、打包构建 stamp、面板版本。打包常量不是独立安装清单校验，明确标 not_observed；缺少独立证据不声称核验安装文件。面板0.9.7不修，仍属S4。会话 ID、同进程模块生命周期内实例重建计数为内存记录；跨进程重启为 coverage_unknown。未取得 OS 进程出生时间就用 null，不用首次诊断时间冒充。
8. 首次手动启用增加五阶段内存账本：enable_request → initialization → assistant_connection → first_observation → first_commit。保留开始/完成时间、结果枚举、耗时、异常类型/安全错误码；最多4次启用、64条事件。不增加等待、重试或自动重载，不改变任何返回值、异常或取消。first_commit 只表示原提交发生，不表示姓名/服务已经完成。

## 改了哪些文件

| 文件 | 职责与核心改动 |
|---|---|
| `runtime/diagnostic_snapshot.py`（新增） | trace侧栏；安全投影、来源对齐、五层health/metrics/events、摘要、唯一序列化、长度控制。核心。 |
| `runtime/diagnostic_session.py`（新增） | trace侧栏；有界内存启用事件、会话/实例元信息、记录器故障隔离。核心。 |
| `runtime/diagnostics.py` | 复用既有白名单字段，统一接入规范对象；未知枚举标记与计数。核心。 |
| `runtime/diagnostic_reader.py` | 原并发读取期限不变，补每个来源开始/结束、耗时和错误类。 |
| `runtime/diagnostic_export.py` | 从规范对象保存JSON，检查ID与复制文本一致，保持独占创建、不覆盖。 |
| `__init__.py` | 仅装配诊断、同ID导出缓存、客户端见证、首次启用被动钩子。原observer分支、await和sleep顺序保留。 |
| `ui/panel_v09.tsx` | 删除独立observation_health框，显示规范快照五行摘要/五层详情，一个复制全部按钮及相同文本的手动复制fallback。 |
| `i18n/zh-CN.json` | 复制按钮及诊断编号文案。 |
| `plugin.toml` | 诊断见证/指定ID导出的action schema与装饰器保持一致，版本未修改。 |
| `layers.toml` | 两个新模块登记trace；baseline文件不改，零新增越界。 |
| `tests/test_f1_diagnostics.py`（新增） | 单份快照、同ID、UTF8边界、隐私哨兵、来源对齐、首次启用与记录器抛错等价性。 |
| `scripts/test_panel_feedback.mjs` | F1面板、16条见证、复制不发新请求、同ID导出；旧S4主动搭话Card断言保留。 |
| 以下14个旧测试文件 | 仅迁移诊断JSON解析入口，及4处 unknown→unmapped_code 的精确预期；不放宽原隐私/大小/业务正确性断言。 |

旧测试文件：`test_bounded_page_handoff_20261021.py`、`test_contract_repairs_20261017.py`、`test_diagnostics.py`、`test_fact_reliability.py`、`test_page_activity_contract_20261020.py`、`test_prepare_failure_trace.py`、`test_recognition_metrics_e1.py`、`test_recording_flow.py`、`test_runtime_stability_20261113.py`、`test_service_handoff.py`、`test_stability_trace.py`、`test_stage2_assessment.py`、`test_query_refactor.py`、`test_d1_handoff_closeout.py`。

附带文档：既有待提交的 `F_DIAGNOSTICS_PLAN_20261124.md`、本报告、`examples/F1_DIAGNOSTIC_FAKE_COPY.txt` 与对应假数据 JSON。没有提交本机实机日志/附件或凭据。

L2a/L2b 识别文件、detectors、adapters、engine、live_refresh、query_reuse、delivery_boundary 均没有改动；原分层归属和白名单不变。

## 面板长什么样

诊断卡保留运行/导出操作。运行后显示检查时间和诊断ID，下方是五个健康摘要徽章；每个楼层有自己的折叠详情（health、metrics、recent_events）。没有第二份独立观察账本框。只有一个“复制全部诊断”按钮；剪贴板受限时显示同一copy_text供手动选择。

```text
诊断编号：aaaaaaaa…（示意）
5F 红  请求与交付  screen_intent_conflict  请求耗时=7500ms
4F 黄  观察与确认  confirming_identity    提交年龄=…，让位=…
3F 黄  检测与区域  existing_events_only   区域计划缺失=…
2F 黄  OCR与排队   existing_events_only   原生OCR=未记录
1F 黄  构建与生命周期 build_mismatch     源码/安装/面板版本各自显示
▸ 5F 请求与交付
▸ 4F 观察与确认
▸ 3F 检测与区域
▸ 2F OCR与排队
▸ 1F 构建与生命周期
[复制全部诊断]
```

实际复制示例见 `examples/F1_DIAGNOSTIC_FAKE_COPY.txt`，全部为构造假数据，非玩家现场。前五行就是面板使用的floor.summary，后面是完整JSON；与配套JSON的diagnostic_id相同。

## 验证记录

- 新增F1定向：31项通过；含原包体积保护的最后定向复验32项通过。
- 第一轮全量：2631通过、10跳过、2失败、0错误，272.794秒。失败为 `test_query_refactor::test_diagnostic_plan_only_exports_whitelisted_requirement_codes` 和 `test_d1_handoff_closeout::test_message_projection_diagnostic_does_not_export_untrusted_text`，均是遗漏迁移的旧unknown预期；精确迁移后两项定向通过，未放宽内容/隐私断言。原报告保留 `f1-full-20261124.xml`。
- 最终全量：2634通过、10跳过、0失败、0错误，208.957秒；seed=11222026。报告 f1-full-final-20261124.xml。
- Ruff：All checks passed。
- 分层门：5项通过、0跳过、0失败；import越界0，baseline零新增。
- 面板Node全套：最终29通过、1失败、0跳过。失败仍为S4既有主动搭话Card查找断言；F1新增和迁移面板用例全部通过，未删除/跳过/放宽旧Card断言。

测试关注：记录器正常/抛错下原返回对象、RuntimeError、CancelledError不变；真实start_auto_observation成功/Err不变；每个启用阶段成功/失败/中途异常；没有诊断OCR/发言/存档写入；旧last_query、不同UTC偏移、当轮新请求、无UI见证；同ID导出不再调用diagnose；128KiB边界合法JSON及size_limited；伪token/UID/聊天/OCR/路径/散列不外泄。

## 边界与后续

F1不宣称E2识别已修复，也不会给出尚未采到的具体拒绝检查。快读最终许可和full解析后未更新可用提交的精确断点，等待F2授权；跨重启保留与重启总次数等待F3。首次手动启用需要重载和面板0.9.7继续作为已知待办，仅新增定位信息。

复制格式是schema2新契约：本地分析工具应读snapshot/json_text；不能继续直接json.loads(copy_text)。冻结的历史安装/录制脚本本轮未重写，也未运行。

本轮不安装、不实机、不录帧；完成草稿PR后停止。