import {
  Page, Card, Grid, Stack, Text, Tip, Alert, StatusBadge, Button,
  ImageUpload, ImagePreview, RadioGroup, Textarea, Field, Accordion,
  RefreshButton, ConfirmDialog, useToast,
} from "@neko/plugin-ui"
import type { HostedAction, PluginSurfaceProps } from "@neko/plugin-ui"

type GameState = {
  primary_state?: string; label?: string; substate?: string; confidence?: string
  evidence?: string[]; summary?: string; fresh?: boolean; has_observation?: boolean; verified?: boolean
}
type Observer = {
  enabled?: boolean; state?: string; message?: string; last_frame_at?: string
  window?: { title?: string; process_name?: string; foreground?: boolean }
  last_delivery?: { submitted?: boolean; reason?: string }
}
type Proposal = {
  proposal_id?: string; recognized_characters?: string[]; uncertain_characters?: string[]
  rejected_characters?: string[]; can_confirm?: boolean; import_mode?: string; note?: string
}
type CompanionState = {
  version?: string; record_count?: number; database_coverage?: Record<string, number>
  data_pack?: { state?: string; message?: string; installed?: boolean; error?: string; meta?: { data_revision?: string; record_count?: number } }
  status?: string; profile?: Record<string, any>; pending_profile_proposal?: Proposal
  current_game_state?: GameState; observer?: Observer
  proactive_companionship?: { enabled?: boolean; mode?: string; idle_scene?: string; idle_interval_seconds?: number }
  vision_backend?: { backend?: string; available?: boolean; last_error?: string }
  local_vision?: { characters?: Array<{ name?: string; entity_id?: string }>; ocr?: { text_preview?: string }; scene?: GameState; page?: { page_type?: string; label?: string; supported?: boolean } }
  runtime?: { stable_scene?: string; pending_scene?: string; pending_scene_confirmations?: number }
  session?: { active?: boolean; context_push_count?: number }
  character_registry?: { record_count?: number; snapshot_date?: string; strict_validation?: boolean }
  correction_count?: number
  disabled_correction_count?: number
  recent_corrections?: Array<{ observed_name?: string; canonical_name?: string }>
  memory_boundary?: string
}

function hasAction(actions: HostedAction[], id: string): boolean {
  return actions.some((action) => action.id === id || action.entry_id === id)
}
function asList(value: any): string[] {
  return Array.isArray(value) ? value.map((item) => String(item)).filter(Boolean) : []
}
function splitNames(value: string): string[] {
  const result: string[] = []
  for (const raw of String(value || "").split(/[、,，\s\n]+/)) {
    const name = raw.trim()
    if (name && !result.includes(name)) result.push(name)
  }
  return result
}
function observerBadge(observer: Observer): { tone: "success" | "warning" | "danger" | "default"; label: string } {
  if (!observer.enabled || observer.state === "stopped") return { tone: "default", label: "自动观察未开启" }
  if (observer.state === "observing") return { tone: "success", label: "正在观察星铁窗口" }
  if (observer.state === "error") return { tone: "danger", label: "画面读取暂时异常" }
  if (observer.state === "paused") return { tone: "warning", label: "自动观察已暂停" }
  return { tone: "warning", label: "正在等待星铁窗口" }
}

export default function HsrCompanionPanel(props: PluginSurfaceProps<CompanionState>) {
  const state = props.state || {}
  const actions = Array.isArray(props.actions) ? props.actions : []
  const observer = state.observer || {}
  const proactive = state.proactive_companionship || { enabled: true }
  const badge = observerBadge(observer)
  const profile = state.profile || {}
  const pending = state.pending_profile_proposal || {}
  const current = state.current_game_state || {}
  const hasVerifiedCurrent = !!current.verified && !!current.has_observation
  const registry = state.character_registry || {}
  const owned = asList(profile.owned_characters)
  const favorites = asList(profile.favorite_characters)
  const targets = asList(profile.training_targets)
  const goals = asList(profile.current_goals)
  const recognized = asList(pending.recognized_characters)
  const uncertain = asList(pending.uncertain_characters)
  const rejected = asList(pending.rejected_characters)
  const hasPending = !!pending.proposal_id
  const corrections = Array.isArray(state.recent_corrections) ? state.recent_corrections : []
  const toast = useToast()

  const dataPack = state.data_pack || {}
  const [running, setRunning] = props.useLocalState("hsr08Running", "")
  const [gameScreenshot, setGameScreenshot] = props.useLocalState<any>("hsr08GameScreenshot", null)
  const [rosterScreenshot, setRosterScreenshot] = props.useLocalState<any>("hsr08RosterScreenshot", null)
  const [importMode, setImportMode] = props.useLocalState("hsr08ImportMode", "merge")
  const [manualNames, setManualNames] = props.useLocalState("hsr08ManualNames", owned.join("、"))
  const [wrongName, setWrongName] = props.useLocalState("hsr08WrongName", recognized[0] || rejected[0] || "")
  const [correctName, setCorrectName] = props.useLocalState("hsr08CorrectName", "")
  const [correctionModality, setCorrectionModality] = props.useLocalState("hsr08CorrectionModality", "voice")
  const [notice, setNotice] = props.useLocalState<{ tone: "info" | "success" | "danger"; text: string } | null>("hsr08Notice", null)
  const [confirmReset, setConfirmReset] = props.useLocalState("hsr08ConfirmReset", false)

  async function call(id: string, args: Record<string, any>, busy: string, success: string) {
    if (!hasAction(actions, id)) {
      toast.error("插件功能还没准备好，请刷新页面")
      return null
    }
    setRunning(busy)
    try {
      const result = await props.api.call(id, args)
      setNotice({ tone: "success", text: success })
      await props.api.refresh()
      toast.success(success)
      return result
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      setNotice({ tone: "danger", text: message })
      toast.error(message)
      return null
    } finally {
      setRunning("")
    }
  }

  async function toggleObservation() {
    if (observer.enabled) await call("stop_auto_observation", {}, "observer", "已停止读取游戏窗口")
    else await call("start_auto_observation", {}, "observer", "自动观察已开启")
  }
  async function toggleProactiveCompanionship() {
    const enabled = proactive.enabled === false
    await call(
      "set_proactive_companionship",
      { enabled },
      "proactive",
      enabled ? "主动陪伴已开启" : "主动陪伴已静音",
    )
  }
  async function submitGameScreenshot() {
    const image = String(gameScreenshot?.dataUrl || "")
    if (!image) {
      toast.error("请先选择一张当前游戏截图")
      return
    }
    await call("submit_game_screenshot", { image_data_url: image }, "game", "插件已在本地完成识别")
  }
  async function submitRosterScreenshot() {
    const image = String(rosterScreenshot?.dataUrl || "")
    if (!image) {
      toast.error("请先选择一张角色列表截图")
      return
    }
    await call("submit_roster_screenshot", { image_data_url: image, import_mode: importMode }, "roster", "插件已完成数据库匹配；确认前不会写入档案")
  }
  async function confirmProposal() {
    const result = await call("confirm_profile_proposal", { proposal_id: pending.proposal_id || "" }, "confirm", "识别结果已保存到玩家档案")
    if (result) setManualNames(recognized.join("、"))
  }
  async function saveCorrection() {
    const observed = (wrongName || recognized[0] || rejected[0] || "").trim()
    const canonical = correctName.trim()
    if (!observed || !canonical) {
      toast.error("请填写认错的名字和正确角色名")
      return
    }
    const result = await call("remember_character_correction", {
      observed_name: observed, canonical_name: canonical, modality: correctionModality,
      note: "玩家在插件面板中明确纠正",
    }, "correction", `已记住：${observed} → ${canonical}`)
    if (result) setCorrectName("")
  }
  async function saveManualNames() {
    const names = splitNames(manualNames)
    if (!names.length) {
      toast.error("请至少填写一个角色名")
      return
    }
    await call("update_player_context", { changes: { owned_characters: names }, mode: "replace", clear_fields: [] }, "manual", "角色档案已更新")
  }
  async function resetAll() {
    await call("reset_demo_data", {}, "reset", "插件保存的数据已清空")
    setGameScreenshot(null); setRosterScreenshot(null); setManualNames(""); setConfirmReset(false)
  }

  return (
    <Page title="星铁游戏搭子 0.8.2" subtitle="可靠事实来自本地页面识别和外部资料组件">
      <Card title="资料组件">
        <Stack>
          <StatusBadge
            tone={dataPack.installed ? "success" : dataPack.state === "error" ? "danger" : "warning"}
            label={dataPack.installed ? "完整资料已就绪" : dataPack.state === "installing" ? "正在准备资料" : dataPack.state === "error" ? "资料准备失败" : "仅启用角色防幻觉名单"}
          />
          <Text>{dataPack.message || "插件会把完整资料安装到插件外部，本体只保留最小角色名单。"}</Text>
          <Text>当前可查询记录：{state.record_count || 0} 条</Text>
          {!dataPack.installed ? <Button tone="primary" disabled={!!running || dataPack.state === "installing"} onClick={() => call("prepare_data_pack", {}, "dataPack", "资料准备任务已启动")}>{running === "dataPack" ? "正在启动……" : "重新准备资料"}</Button> : null}
          <Tip>资料组件带固定版本、哈希校验、来源和许可证；更新插件不会把整套资料重复塞进安装包。</Tip>
        </Stack>
      </Card>
      <Card title="连接游戏">
        <Stack>
          <StatusBadge tone={badge.tone} label={badge.label} />
          <Text>{observer.message || "开启后，插件会自动寻找《崩坏：星穹铁道》窗口。"}</Text>
          {observer.window?.title ? <Text>已找到：{observer.window.title}</Text> : null}
          <StatusBadge
            tone={proactive.enabled === false ? "default" : "success"}
            label={proactive.enabled === false ? "主动陪伴已静音" : "主动陪伴已开启"}
          />
          <Button tone={observer.enabled ? "default" : "success"} disabled={!!running} onClick={toggleObservation}>
            {running === "observer" ? "请稍候……" : observer.enabled ? "停止自动观察" : "开始自动观察"}
          </Button>
          <Tip>复用 N.E.K.O. 内置 RapidOCR，只读取前台星铁窗口；切到其他应用或最小化时暂停。战斗、跃迁、结算、队伍准备、确认角色和探索空闲等合适时机会触发简短陪伴，剧情和普通菜单只静默同步。</Tip>
        </Stack>
      </Card>

      <Grid cols={2}>
        <Card title="猫娘看到的当前状态">
          <Stack>
            <StatusBadge tone={hasVerifiedCurrent && current.fresh && current.primary_state !== "unknown" ? "success" : hasVerifiedCurrent ? "warning" : "default"} label={hasVerifiedCurrent ? `${current.label || "暂时无法判断"}${current.fresh ? "" : " · 已过期"}` : "还没有可靠判断"} />
            {current.substate ? <Text>具体状态：{current.substate}</Text> : null}
            {current.summary ? <Text>{current.summary}</Text> : null}
            {asList(current.evidence).length ? <Tip>依据：{asList(current.evidence).join("；")}</Tip> : null}
            <RefreshButton label="刷新状态" />
          </Stack>
        </Card>
        <Card title="玩家语境">
          <Stack>
            <StatusBadge tone={registry.strict_validation ? "success" : "warning"} label={registry.strict_validation ? `严格角色库已启用 · ${registry.record_count || 0} 名` : "角色库未就绪"} />
            <Text>拥有角色：{owned.length ? owned.join("、") : "暂未记录"}</Text>
            <Text>喜欢：{favorites.length ? favorites.join("、") : "暂未记录"}</Text>
            <Text>正在培养：{targets.length ? targets.join("、") : "暂未记录"}</Text>
            <Text>近期目标：{goals.length ? goals.join("；") : "暂未记录"}</Text>
          </Stack>
        </Card>
      </Grid>

      <Card title="本地识别与数据库">
        <Stack>
          <StatusBadge tone={state.vision_backend?.available ? "success" : "warning"} label={state.vision_backend?.available ? "共享 OCR 已就绪" : "共享 OCR 暂不可用"} />
          <Text>资料覆盖：角色 {state.database_coverage?.character || 0} · 形态 {state.database_coverage?.character_form || 0} · 技能 {state.database_coverage?.character_skill || 0} · 星魂 {state.database_coverage?.character_rank || 0} · 行迹 {state.database_coverage?.character_trace || 0} · 光锥 {state.database_coverage?.light_cone || 0} · 遗器套装 {state.database_coverage?.relic_set || 0}</Text>
          <Text>页面识别：{state.local_vision?.page?.label || "暂无"}{state.local_vision?.page?.supported === false ? " · 暂不支持" : ""}</Text>
          <Text>稳定场景：{state.runtime?.stable_scene || "unknown"}{state.runtime?.pending_scene ? ` · 正在确认 ${state.runtime.pending_scene}（${state.runtime.pending_scene_confirmations || 0}/2）` : ""}</Text>
          <Text>本地确认角色：{Array.isArray(state.local_vision?.characters) && state.local_vision!.characters!.length ? state.local_vision!.characters!.map((item) => item.name).filter(Boolean).join("、") : "暂无"}</Text>
          {state.local_vision?.ocr?.text_preview ? <Tip>最近 OCR：{state.local_vision.ocr.text_preview}</Tip> : null}
        </Stack>
      </Card>

      {notice ? <Alert tone={notice.tone}>{notice.text}</Alert> : null}
      {hasPending ? (
        <Card title="识别结果需要你确认">
          <Stack>
            <Alert tone={rejected.length ? "danger" : uncertain.length ? "warning" : "success"}>{rejected.length ? "角色库拦截了不存在或未收录的名字。" : uncertain.length ? "有些角色还不能确定，请先核对。" : "所有候选都已通过角色库校验。"}</Alert>
            <Text>确认识别：{recognized.length ? recognized.join("、") : "暂无"}</Text>
            {uncertain.length ? <Text>不确定：{uncertain.join("、")}</Text> : null}
            {rejected.length ? <Text>已拦截：{rejected.join("、")}</Text> : null}
            <Grid cols={2}>
              <Button tone="success" disabled={!!running || !recognized.length || pending.can_confirm === false} onClick={confirmProposal}>{running === "confirm" ? "正在保存……" : "确认并保存"}</Button>
              <Button tone="default" disabled={!!running} onClick={() => call("discard_profile_proposal", {}, "discard", "本次识别已放弃")}>{running === "discard" ? "正在处理……" : "这次不保存"}</Button>
            </Grid>
          </Stack>
        </Card>
      ) : null}

      <Accordion title="识别错了？告诉插件正确答案">
        <Stack>
          <Text>玩家的明确纠正会优先于模型猜测；正确名字仍必须存在于角色库。</Text>
          <Field label="刚才认成 / 听成了"><Textarea value={wrongName} placeholder="例如：知更鸟，或语音转写出的错字" onChange={setWrongName} /></Field>
          <Field label="正确角色是"><Textarea value={correctName} placeholder="例如：爻光" onChange={setCorrectName} /></Field>
          <RadioGroup value={correctionModality} options={[
            { value: "voice", label: "语音听错" }, { value: "chat", label: "文字错字 / 别名" }, { value: "vision", label: "画面认错" },
          ]} onChange={setCorrectionModality} />
          <Button tone="success" disabled={!!running || !wrongName.trim() || !correctName.trim()} onClick={saveCorrection}>{running === "correction" ? "正在记住……" : "记住正确答案"}</Button>
          <Tip>语音和文字别名可以长期生效；视觉纠错只学习当前页面中的角色区域，不会把一个真实角色名全局改成另一个角色。</Tip>
        </Stack>
      </Accordion>

      <Accordion title="自动识别不方便时，手动提供截图">
        <Stack>
          <Text>这是备用入口。自动观察关闭、游戏无法前台运行，或需要导入角色列表时再使用。</Text>
          <Field label="当前游戏画面"><ImageUpload value={gameScreenshot} label="选择当前画面" placeholder="PNG、JPEG 或 WebP" accept="image/png,image/jpeg,image/webp" maxBytes={10485760} onChange={setGameScreenshot} /></Field>
          {gameScreenshot ? <ImagePreview value={gameScreenshot} alt="待识别的当前游戏画面" /> : null}
          <Button tone="default" disabled={!!running || !gameScreenshot} onClick={submitGameScreenshot}>{running === "game" ? "正在判断……" : "识别当前状态"}</Button>
          <Field label="角色列表截图"><ImageUpload value={rosterScreenshot} label="选择角色列表" placeholder="截取角色列表页面" accept="image/png,image/jpeg,image/webp" maxBytes={10485760} onChange={setRosterScreenshot} /></Field>
          {rosterScreenshot ? <ImagePreview value={rosterScreenshot} alt="待识别的角色列表" /> : null}
          <RadioGroup value={importMode} options={[
            { value: "merge", label: "部分角色 / 补充到档案" }, { value: "replace", label: "完整列表 / 替换旧档案" },
          ]} onChange={setImportMode} />
          <Button tone="primary" disabled={!!running || !rosterScreenshot} onClick={submitRosterScreenshot}>{running === "roster" ? "正在识别……" : "识别角色列表"}</Button>
        </Stack>
      </Accordion>

      <Accordion title="角色档案需要手动补充时">
        <Stack>
          <Text>仅作为识别失败时的备用方式。每个名字保存前都会经过角色库校验。</Text>
          <Textarea value={manualNames} placeholder="角色名用顿号、逗号、空格或换行分隔" onChange={setManualNames} />
          <Button tone="default" disabled={!!running} onClick={saveManualNames}>{running === "manual" ? "正在保存……" : "保存角色列表"}</Button>
        </Stack>
      </Accordion>

      <Accordion title="插件设置">
        <Stack>
          <Text>自动观察：{observer.enabled ? "已开启" : "已关闭"}</Text>
          <Text>主动陪伴：{proactive.enabled === false ? "已静音（仍会同步状态）" : "已开启"}</Text>
          <Text>陪玩语境：{state.session?.active ? "已同步" : "未开启"}</Text>
          <Text>已记录纠错：{state.correction_count || 0} 条</Text>
          {state.disabled_correction_count ? <Text>已安全停用旧纠错：{state.disabled_correction_count} 条</Text> : null}
          <Text>最近纠错：{corrections.length ? corrections.map((item) => `${item.observed_name}→${item.canonical_name}`).join("；") : "暂无"}</Text>
          <Text>角色库快照：{registry.snapshot_date || "未知日期"}</Text>
          <Button tone="primary" disabled={!!running} onClick={() => call("refresh_companion_context", {}, "refresh", "当前档案和规则已重新同步")}>重新同步给猫娘</Button>
          <Button tone="default" disabled={!!running} onClick={toggleProactiveCompanionship}>
            {running === "proactive" ? "正在切换……" : proactive.enabled === false ? "开启主动陪伴" : "暂时静音主动陪伴"}
          </Button>
          <Button tone="danger" disabled={!!running} onClick={() => { setConfirmReset(true) }}>清空插件数据</Button>
        </Stack>
      </Accordion>

      <ConfirmDialog open={confirmReset} title="确定清空吗？" message="这会关闭自动观察，并清空本插件保存的玩家档案、纠错、状态和经历；不会影响 N.E.K.O 本体或游戏客户端。" tone="danger" confirmLabel="确定清空" cancelLabel="取消" onConfirm={resetAll} onCancel={() => setConfirmReset(false)} />
      <Tip>{state.memory_boundary || "插件档案可以修改和清空，不等同于 N.E.K.O 本体的长期情感记忆。"}</Tip>
    </Page>
  )
}
