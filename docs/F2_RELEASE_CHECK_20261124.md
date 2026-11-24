# F2 Release check 失败定位与最小修复（2026-11-24）

范围：PR #25，修前 `2ad3993`。只修该次 push CI 的 Release check，不修改任何诊断、识别、交付、调度、配置或业务判断。

## 原始 CI

- [F1 绿色 push CI](https://github.com/WhiteFrostune/n.e.k.o_plugin_hsr_companion/actions/runs/45402657377)：F1 `02610e6`，引用 N.E.K.O main `fed1687`。
- [F2 失败 push CI](https://github.com/WhiteFrostune/n.e.k.o_plugin_hsr_companion/actions/runs/45417499191)：F2 `2ad3993`，引用并检出 main `f665a41`。Ruff、挂载与依赖同步通过，Release check 内的 pytest 失败；未进入后续正常打包回执。
- 原失败：8 failed / 2669 passed / 120 skipped。4 个全进程时钟替换被宿主守卫拦下；4 个 Git 历史读取失败。

## 同一 main 下的本地对照

两版都在未修改的隔离 main `f665a41a3d2e00991e93a81085c6909374e5733e` 目录运行：

```text
uv run python -m plugin.neko_plugin_cli.cli check -r hsr_companion
```

本地两版统一取各提交的 git archive，不带插件自己的 .git 和未跟踪历史素材；这是额外覆盖的无 Git 源码场景。实际 CI 使用 checkout 默认 depth=1，再由 copytree 连同浅检出的 .git 一起挂载，不能把二者目录布局写成相同。两种场景都没有测试所需的完整 F1 历史；原 F1 / F2 本地目录均保留，未删除。

本机为 Windows / Python 3.11.15 / pytest 9.0.2；原 CI 为 Linux / Python 3.11.18 / pytest 9.1.1。为避免同步或改动运行中的 N.E.K.O 环境，本地两版均只读复用同一依赖环境：`UV_NO_SYNC=1`、`UV_PROJECT_ENVIRONMENT=<FROZEN_TEST_ENV>`、`PYTHONPATH=<NEKO_MAIN>`、`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`。隔离树未检出无关资源/部分可选模块。本地不声称与 CI 的操作系统和全部依赖完全一致，最终仍以实际 push CI 补验。

| 检查 | exit | 实际结果 | 完整脱敏输出 |
|---|---|---|---|
| F1 首次环境预检 | 1 | Windows CLI 使用 GBK 解码 UTF-8 子测试输出失败，不能据此评价 pytest；记录保留 | [输出](release-check-20261124/f1-encoding-preflight.txt) |
| F1 `02610e6`，有效 UTF-8 对照 | 1 | 2555 passed / 88 skipped / 1 failed：旧的 `test_final_receipt_reports_boundary_reason_not_just_empty_payload` 期望 partial、实际 retained | [输出](release-check-20261124/f1-main-before.txt) |
| 同一 F1 旧断言单独运行 | 0 | 1 passed；原断言未修改。全量失败的具体原因仍未定，不能凭单例通过推断线程或前序污染原因 | [输出](release-check-20261124/f1-old-assertion-isolated.txt) |
| F2 `2ad3993`，相同 UTF-8 条件 | 1 | 2701 passed / 88 skipped / 8 failed，正好复现原 CI 的两类 8 项；没有出现 F1 那条旧断言失败 | [输出](release-check-20261124/f2-main-before.txt) |
| F2 修后 | 0 | `tests=passed`，打包成功，`payload_hash_verified=True`；总耗时 236.031 秒 | [输出](release-check-20261124/f2-main-after.txt) |

成功时 CLI 本身只输出 `tests=passed`，不会转述内部 pytest 数字；独立全量门另行如实记录。完整输出仅替换本机路径、用户名等标识，未删减失败正文或断言。

## 归类与定因

**本次 CI 主因属于 a：F2 新增测试的环境依赖。** 不是诊断逻辑、docs JSON、清单/schema 或文件大小导致的拒绝。

1. `tests/test_f2_rejection_trace.py` 的 worker_expired / review_expired 两种情况，各有 broken=False/True，共 4 项。原来替换 `compact.time.monotonic`；compact.time 是 stdlib time 对象，因此改了全进程时钟。main 的 `plugin/conftest.py` 注册 `tests/clock_guard.py`，在测试返回、monkeypatch teardown 前检查并拒绝这种替换。
2. 同文件 AST 等价检查的 4 个参数项调用 `git show 02610e6:<file>`。本机完整插件 worktree 能读该历史；CI 的 checkout 日志明确是 --depth=1，随后 copytree 保留浅检出的 .git，该检出不含父提交 02610e6，故返回 exit 128。另一个本地归档场景不带插件 .git，Git 向上找到 N.E.K.O 仓库，同样没有该对象。测试不应依赖这两种布局都不保证的历史。
3. 原 standalone 全量门没有经过同一个宿主父目录 conftest，且本地插件 Git 历史齐全，因而未发现以上可移植性问题。这是 F2 测试编写/验收环境覆盖的缺口，不应归因于 OCR。

**排除 b：** 两次原 CI main 提交 `fed1687` → `f665a41` 之间只改了 `static/css/api_key_settings.css`。Release check、plugin/conftest.py、clock_guard.py、fake_clock.py、pytest.ini 的 Git blob 全部相同，见 [逐文件证据](release-check-20261124/rules-comparison.json)。没有“最近新增这条规则”的提交，故不改 N.E.K.O，也不改规则或绕过守卫。

**另有 c，但不混作这次 CI 根因：** Windows GBK 解码障碍已仅用检查进程 UTF-8 环境处理；F1 的旧 partial/retained 断言全量失败、单项通过，具体运行条件尚未查透，本轮保留不修。不同平台、依赖和缺少未跟踪历史素材使本地与 CI 的 skip 数不同；未添加 skip/xfail/host_clock_guard 标记绕过本次 8 项。

## 最小修改

- 仅改 `tests/test_f2_rejection_trace.py`：把两处时钟替换限定到 compact 模块本地名称，保留相同 deadline+1 和原结果/分支断言；新增断言确认 stdlib time.monotonic 未改变。
- 新增 `tests/fixtures/f2_business_baseline_02610e6.json`：从固定 F1 完整提交逐一提取原本被比较的 5 个函数源码，保留来源提交和文件校验值。已逐函数验证 AST 与原 `git show` 结果完全一致。测试仍使用原来的精确 AST 等式，不执行这些基线源码、不跳过比较、不自动改基线；不再依赖运行时 Git 历史。
- 其余新增文件仅本报告与脱敏证据。没有改 pyproject、打包规则、工作流、版本号、runtime、UI 或业务代码。

## 验证

真实宿主时钟守卫 + 无插件 Git 历史的隔离目录：50 passed / 103 deselected，覆盖原失败两类及关联快读分支；原 8 项均在其中。没有降低阈值、期限或删除原断言。

提交前本候选实际验证：

- 完整 Release check：exit 0，236.031 秒，测试/打包/payload hash 校验通过；CLI 成功输出不包含 pytest 统计。
- 独立全量 pytest（seed 11222026）：2787 passed / 10 skipped / 0 failed / 0 errors，2797 项，265.175 秒。
- Ruff：exit 0，All checks passed。
- 分层门：5 passed / 0 skipped / 0 failed，1.441 秒；baseline 仍为空，零新增。
- 5 个冻结基线函数再次逐一与 F1 提交 AST 对照一致；生产源码、清单、工作流与分层白名单零修改。

上述本地门通过后推送同一 F2 分支，由新的 push CI 再验证 Linux/浅克隆环境。最终实际 CI 链接与结论补在 PR #25 和本轮报告回复；原失败运行保留，不重跑覆盖。PR 保持草稿，基分支仍为 F1，不安装、不合并、不推 main。
