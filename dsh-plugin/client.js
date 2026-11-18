/**
 * HitMind — browser half for DeepSeek Harness (DSH).
 *
 * A schema-driven settings card: it renders editable controls for the plugin's
 * volatile Config directly from the settings service's projected schema, and
 * writes edits back through the native settings form (`ctx.configForms`). So the
 * DSH Plugins page can view AND edit HitMind's configuration (agentId, dbPath,
 * pythonBin, daemonBin, embed*, inject/capture/compaction toggles, injectTopK …)
 * like the official settings cards — but generic, not one-per-field.
 *
 * Emitted as a lazy-CJS factory (the client module system's required format):
 * it only registers a factory; the body materializes on first import. All module
 * requests are platform-seeded (react, react/jsx-runtime,
 * @deepseek-ai/dsh-client-ui-primitives), so `dsh.client.external` is empty.
 */
window.__ModuleLoader__.load({
  id: 'dsh-hitmind',
  factory: (require) => {
    var module = { exports: {} }
    var exports = module.exports
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' })

    const React = require('react')
    const primitives = require('@deepseek-ai/dsh-client-ui-primitives')
    const h = React.createElement
    const { Component, useState, useMemo, useSyncExternalStore } = React
    const SettingsForm = primitives.SettingsForm
    const SettingsValueField = primitives.SettingsValueField
    const Switch = primitives.Switch

    /** Settings namespace = profile entry id (see the host Config / cordis.patch.yml). */
    const NS = 'hitmind'
    /** Bundle package name — the key the bundle's own page uses to match its config slot. */
    const PKG = 'dsh-hitmind'

    /** Copy shown in the card (Chinese; the DSH UI here runs in zh). */
    const L = {
      title: 'HitMind 记忆配置',
      summary: 'HitMind 长期记忆：工具、自动注入、会话/压缩捕获，以及 Python sidecar 与嵌入服务配置。改动即时保存到 profile 并热更新（无需重挂插件）。',
      unavailable: '该配置在当前部署中不可用',
      readOnly: '只读',
      saveFailed: '保存失败',
      save: '保存',
      saving: '保存中…',
      overridden: '已覆盖',
      reset: '重置',
      invalidNumber: '请输入有效数字',
      loading: '正在加载配置…',
      noService: '配置服务不可用（configForms 未提供）',
      renderError: '配置卡渲染出错：',
    }

    /** Field labels/hints for the known Config keys; unknown keys fall back to the key. */
    // DSH-operational fields (connection / runtime / capture switches).
    const OPERATIONAL_META = {
      agentId: { label: 'Agent ID', hint: '多 Agent 隔离：本实例写入/召回使用的 agent_id', group: '连接与运行' },
      injectEnabled: { label: '自动注入', hint: '每轮对话前自动注入相关记忆', group: '连接与运行' },
      captureEnabled: { label: '捕获会话（chat 逐条）', hint: 'chat 模式下从会话事件累计并批量抽取记忆', group: '连接与运行' },
      compactionEnabled: { label: '捕获压缩摘要', hint: '对话压缩时把摘要作为 episode 全量抽取', group: '连接与运行' },
      workMode: { label: 'work 模式（全局）', hint: '开=不逐条捕获、仅靠压缩摘要；关=chat 逐条捕获+批次抽取', group: '连接与运行' },
      captureFlushMs: { label: '批量抽取延迟(ms)', hint: '会话空闲后触发抽取的防抖毫秒数', group: '连接与运行' },
      diag: { label: '诊断日志', hint: '打印 session/event 形状，便于排查', group: '连接与运行' },
    }
    // Core tunables generated from hitmind/settings_spec.py (label/hint/group), so the
    // DSH panel is grouped identically to the Hermes panel. Keys are camelCase.
    const SPEC_META = {
      syncRoles: { label: '自动捕获角色', hint: '哪些说话角色会被自动捕获为记忆。', group: '记忆抽取' },
      syncUserMin: { label: 'user 消息最小长度（字符）', hint: '短于此长度的 user 消息不捕获。', group: '记忆抽取' },
      syncAssistantMin: { label: 'assistant 消息最小长度（字符）', hint: '短于此长度的 assistant 消息不捕获。', group: '记忆抽取' },
      consolidateEveryTurns: { label: 'chat 批次轮数阈值', hint: '未处理消息数 ≥ 2×此值 时标记待处理批次（闲时抽取）。', group: '记忆抽取' },
      memoryMaxChars: { label: '单条记忆最大长度（字符）', hint: '每条记忆内容的最大字符数；超出时拆分为多条（0=不限）。', group: '记忆抽取' },
      timeNormalize: { label: '时间归一化（相对→绝对日期）', hint: '写入时把“昨天/下周三”按逻辑时间补成绝对日期；确定性、零 LLM。', group: '记忆抽取' },
      tzHours: { label: '时区（auto 或小时偏移）', hint: 'auto=读系统时区；也可填 8 / 5.5 / 5:30。', group: '记忆抽取' },
      skipContexts: { label: '跳过捕获的上下文', hint: '逗号分隔；这些上下文中的消息不捕获。', group: '记忆抽取' },
      chatCooldownSeconds: { label: '失败冷却（秒）', hint: 'chat 批次连续失败后冷却多久重试（保留消息）。', group: '记忆抽取' },
      idleConsolidateSeconds: { label: '闲时阈值（秒）', hint: '空闲 ≥ 此秒数才处理后台任务；0 关闭闲时整理。', group: '记忆抽取' },
      idlePollSeconds: { label: '后台轮询间隔（秒）', hint: '闲时线程检查待办的间隔（最小 15）。', group: '记忆抽取' },
      injectTopK: { label: '召回条数（top_k）', hint: '每轮注入/查询返回的最大命中条数。', group: '召回' },
      denseFloor: { label: '向量最低相似度（余弦）', hint: 'dense 检索余弦门槛，低于则丢弃；不同 embedding 分布不同需调整。', group: '召回' },
      topicExpandCap: { label: '同主题扩展：每主题保留条数', hint: '每个被展开 topic 最多保留的总条数。', group: '召回' },
      topicExpandHits: { label: '同主题扩展：取前 N 个 topic', hint: '对前 N 条命中的 topic 做扩展。', group: '召回' },
      relWeight: { label: '相关性权重（0–1）', hint: '得分 = 相关度×此 + 重要性×importanceWeight + 新鲜度×余下。', group: '召回' },
      importanceWeight: { label: '重要性权重（0–1）', hint: '得分中重要性的权重（默认 0.8/0.1/0.1）。', group: '召回' },
      importancePersona: { label: 'persona 重要性（0–1）', hint: 'persona 类默认重要性。', group: '重要性' },
      importanceFact: { label: 'fact 重要性（0–1）', hint: 'fact 类默认重要性。', group: '重要性' },
      importanceEvent: { label: 'event 重要性（0–1）', hint: 'event 类默认重要性。', group: '重要性' },
      importanceProcedure: { label: 'procedure 重要性（0–1）', hint: 'procedure 类默认重要性。', group: '重要性' },
      topicMinCluster: { label: '每簇够格线（条）', hint: '簇成员 ≥ 此值才送 LLM 判定是否同一件事。', group: 'Topic 聚类' },
      clusterMinSize: { label: '触发积压门槛（条）', hint: '待整理成员合计 ≥ 此值才启动一轮 topic 整理。', group: 'Topic 聚类' },
      consolidateMinWorking: { label: '定稿最小待办数（条）', hint: '待处理记忆数 ≥ 此值才值得启动定稿（省 LLM）。', group: 'Topic 聚类' },
      clusterCosFloor: { label: '聚类余弦门槛（0–1）', hint: '两条记忆并入同簇的最小余弦相似度。', group: 'Topic 聚类' },
      topicMaxMembers: { label: '单 topic 最大记忆条数', hint: '一个 topic 簇最多成员数，超过另起新簇。', group: 'Topic 聚类' },
      clusterScanWindow: { label: '预聚类比较窗口（条）', hint: '新记忆只与最近 N 条比较以决定归簇。', group: 'Topic 聚类' },
      episodeTtlDays: { label: 'episode 保留天数', hint: '压缩摘要 episode 保留天数，到期物理删除。', group: '生命周期管理' },
      rawTtlDays: { label: '原始捕获保留天数', hint: '逐轮原始记忆保留天数（0=不清理）。', group: '生命周期管理' },
      capacityWarn: { label: '容量告警阈值（条）', hint: 'active 记忆超过此值仅告警，不删除。', group: '生命周期管理' },
      versionKeep: { label: '记忆保留历史版本数', hint: '多次更新时保留多少条旧版本。', group: '生命周期管理' },
      reviewTriggerMin: { label: '待复核触发阈值（条）', hint: '待复核记忆超过此条数时闲时用 LLM 判定并更新版本。', group: '生命周期管理' },
      ttlEventDays: { label: 'event 过期天数', hint: 'event 类默认过期天数。', group: '生命周期管理' },
      ttlPersonaDays: { label: 'persona 过期天数（0=永不）', hint: 'persona 类默认过期天数；0 不按时间过期。', group: '生命周期管理' },
      ttlFactDays: { label: 'fact 过期天数（0=永不）', hint: 'fact 类默认过期天数；0 不按时间过期。', group: '生命周期管理' },
      ttlProcedureDays: { label: 'procedure 过期天数（0=永不）', hint: 'procedure 类默认过期天数；0 不按时间过期。', group: '生命周期管理' },
      halfLifeEventDays: { label: 'event 半衰期（天）', hint: 'event 类新鲜度半衰期。', group: '生命周期管理' },
      halfLifePersonaDays: { label: 'persona 半衰期（天）', hint: 'persona 类新鲜度半衰期。', group: '生命周期管理' },
      halfLifeFactDays: { label: 'fact 半衰期（天）', hint: 'fact 类新鲜度半衰期。', group: '生命周期管理' },
      halfLifeProcedureDays: { label: 'procedure 半衰期（天）', hint: 'procedure 类新鲜度半衰期。', group: '生命周期管理' },
      embeddingBaseUrl: { label: 'Embedding 服务地址（/v1）', hint: '如 http://127.0.0.1:8091/v1；留空退化为纯 BM25。', group: '模型配置' },
      embeddingModel: { label: 'Embedding 模型名', hint: '本地 embedding 服务的模型名。', group: '模型配置' },
      embeddingApiKey: { label: 'Embedding API Key', hint: '本地服务通常留空；真密钥请放宿主环境，不写入配置。', group: '模型配置' },
      rerankEnabled: { label: '启用重排（cross-encoder）', hint: '对召回短名单用 rerank 重排后再取 top_k；不可用自动跳过。', group: '模型配置' },
      rerankModel: { label: '重排模型 id', hint: '本地 /rerank 端点使用的 cross-encoder 模型 id。', group: '模型配置' },
      rerankPool: { label: '重排候选池（条）', hint: '取前多少条一阶段候选交给 rerank 重排。', group: '模型配置' },
      llmTimeoutS: { label: 'LLM 调用超时（秒）', hint: '单次宿主 LLM 调用的硬超时。', group: '模型配置' },
      consolidateLlmEnabled: { label: '启用 LLM 定稿', hint: '关闭则只做零 LLM 预聚类，不做 topic 定稿。', group: '模型配置' },
      bgRunner: { label: '启用闲时后台线程', hint: '关闭后 chat 批次与 topic 整理不会自动运行。', group: '模型配置' },
    }
    const META = Object.assign({}, OPERATIONAL_META, SPEC_META)
    // Display order of groups (Hermes parity); unknown groups append at the end.
    const GROUP_ORDER = ['连接与运行', '记忆抽取', '召回', '重要性', 'Topic 聚类', '生命周期管理', '模型配置']

    /** Read a serialized Schemastery node's default value. */
    function defOf(node) {
      return node && node.meta && Object.prototype.hasOwnProperty.call(node.meta, 'default')
        ? node.meta.default
        : undefined
    }

    /** Resolve a serialized Schemastery node. Wire schemas are `{uid, refs:{id:node}}`
     *  envelopes where dict values are ref ids; inline nodes are passed through. */
    function resolveNode(schema, ref) {
      if (ref === undefined || ref === null) return undefined
      if (typeof ref === 'object') return ref
      const refs = schema && schema.refs
      return refs ? refs[String(ref)] : undefined
    }

    /** Flatten the top-level object schema into field descriptors (key/type/default). */
    function fieldsFromSchema(schema) {
      const out = []
      if (!schema) return out
      const root = (schema.refs && schema.uid !== undefined)
        ? (schema.refs[String(schema.uid)] || schema)
        : schema
      const dict = root && root.dict
      if (!dict || typeof dict !== 'object') return out
      for (const key of Object.keys(dict)) {
        const node = resolveNode(schema, dict[key]) || {}
        const t = node.type
        if (t !== 'string' && t !== 'number' && t !== 'integer' && t !== 'boolean') continue
        out.push({
          key,
          type: t === 'integer' ? 'number' : t,
          default: defOf(node),
          label: (META[key] && META[key].label) || key,
          hint: (META[key] && META[key].hint) || '',
          group: (META[key] && META[key].group) || '其它',
        })
      }
      return out
    }

    /** Whether a staged draft text equals the current stored value for a field. */
    function fieldEquals(fields, text, key, value) {
      const f = fields.find((x) => x.key === key)
      if (!f) return true
      const cur = (key in value) ? value[key] : f.default
      if (text.trim() === '') return cur === undefined || cur === null || cur === ''
      if (f.type === 'number') return String(cur) === String(Number(text)) && Number.isFinite(Number(text))
      return String(cur) === text
    }

    /** Minimal error boundary so a render error is shown instead of a blank panel. */
    class Boundary extends Component {
      constructor(props) { super(props); this.state = { err: null } }
      static getDerivedStateFromError(err) { return { err } }
      componentDidCatch(err) { try { console.error('[hitmind] card render error', err) } catch (e) {} }
      render() {
        if (this.state.err) {
          return h('pre', { style: { color: '#c00', whiteSpace: 'pre-wrap', fontSize: 12 } },
            L.renderError + String(this.state.err && this.state.err.stack || this.state.err))
        }
        return this.props.children
      }
    }

    /**
     * The settings card body. Reads schema + live values from the native settings
     * services and writes edits back through `form.set` / `form.mutate`.
     * @param props - `view` plus injected `form` (ConfigForm) and `describe` (settings describe mirror).
     */
    function HitMindCardBody(props) {
      const form = props.form
      const describe = props.describe

      // Stable bound readers (the stores are class instances: never pass unbound methods).
      const formGet = useMemo(() => (() => form.getSnapshot()), [form])
      const formSub = useMemo(() => ((cb) => form.subscribe(cb)), [form])
      const descGet = useMemo(() => (() => describe.getSnapshot()), [describe])
      const descSub = useMemo(() => ((cb) => describe.subscribe(cb)), [describe])

      // Live schema (field list) from the describe mirror.
      const mirrorSnap = useSyncExternalStore(descSub, descGet)
      const view = mirrorSnap && mirrorSnap.view
      const nsView = view && view.namespaces && view.namespaces.find((n) => n.ns === NS)
      const schema = nsView ? nsView.schema : undefined
      const fields = useMemo(() => fieldsFromSchema(schema), [schema])
      const sig = fields.map((f) => f.key + ':' + f.type).join(',')

      // Live values / revision / writability from the config form.
      const formSnap = useSyncExternalStore(formSub, formGet)
      const value = (formSnap && formSnap.value) || {}
      const user = (formSnap && formSnap.user) || {}
      const ready = !!(formSnap && formSnap.status === 'ready')
      const writable = !!(formSnap && formSnap.writable)
      const revision = formSnap && formSnap.revision

      const [rawDrafts, setDrafts] = useState({}) // key -> staged string
      const [saving, setSaving] = useState(false)
      const [failed, setFailed] = useState(false)
      const [errMsg, setErrMsg] = useState('')
      const [togglePending, setTogglePending] = useState(false)
      // Reset staged drafts when the field set changes underneath us.
      const staged = useMemo(() => ({}), [sig]) && rawDrafts // keep rawDrafts, keyed by sig for reset semantics

      const editField = (key, text) => { setFailed(false); setDrafts((d) => ({ ...d, [key]: text })) }
      const clearField = (key) => { setFailed(false); setDrafts((d) => ({ ...d, [key]: '' })) }

      const invalidKeys = {}
      for (const f of fields) {
        if (!(f.key in rawDrafts)) continue
        const text = rawDrafts[f.key]
        if (f.type === 'number' && String(text).trim() !== '' && !Number.isFinite(Number(text))) invalidKeys[f.key] = true
      }
      const dirty = Object.keys(rawDrafts).some((k) => !fieldEquals(fields, rawDrafts[k], k, value))
      const invalid = Object.keys(invalidKeys).length > 0

      const save = async () => {
        const ops = []
        for (const f of fields) {
          if (!(f.key in rawDrafts)) continue
          const text = rawDrafts[f.key]
          if (text.trim() === '') { ops.push({ op: 'unset', path: [f.key] }); continue }
          if (f.type === 'number') { if (Number.isFinite(Number(text))) ops.push({ op: 'set', path: [f.key], value: Number(text) }) }
          else ops.push({ op: 'set', path: [f.key], value: text })
        }
        if (!ops.length) { setDrafts({}); return }
        setSaving(true); setFailed(false); setErrMsg('')
        try {
          let ok = false
          try {
            ok = await form.mutate(ops, revision)
          } catch (e) {
            setFailed(true); setErrMsg(String((e && (e.message || e.cause)) || e))
            return
          }
          if (ok) { setDrafts({}); setErrMsg('') }
          else { setFailed(true); setErrMsg('保存被拒绝（配置被覆盖或校验失败）') }
        } finally { setSaving(false) }
      }

      const toggle = async (key, next) => {
        setFailed(false); setErrMsg(''); setTogglePending(true)
        try { const ok = await form.set(key, next); if (!ok) { setFailed(true); setErrMsg('set 返回 false（Host 拒绝）') } }
        catch (e) { setFailed(true); setErrMsg(String((e && (e.message || e.cause)) || e)) } finally { setTogglePending(false) }
      }

      if (!ready) return h('p', { style: { opacity: 0.7 } }, L.loading)

      const disabled = !writable || saving || togglePending
      const formState = { available: ready, writable, dirty, invalid, saving: saving || togglePending, failed }
      const labels = { unavailable: L.unavailable, readOnly: L.readOnly, saveFailed: L.saveFailed, save: L.save, saving: L.saving }

      const renderField = (f) => {
        if (f.type === 'boolean') {
          const cur = (f.key in value) ? value[f.key] : f.default
          return h('div', { key: f.key, style: { display: 'flex', alignItems: 'center', gap: 10, padding: '6px 0' } },
            h(Switch, { checked: !!cur, onChange: (n) => { toggle(f.key, n) }, label: f.label, disabled }),
            h('span', null,
              h('div', { style: { fontWeight: 500 } }, f.label),
              f.hint ? h('div', { style: { fontSize: 12, opacity: 0.65 } }, f.hint) : null))
        }
        const cur = (f.key in value) ? value[f.key] : f.default
        const text = (f.key in rawDrafts) ? rawDrafts[f.key] : (cur === undefined || cur === null ? '' : String(cur))
        return h(SettingsValueField, {
          key: f.key,
          id: 'hitmind-cfg-' + f.key,
          label: f.label,
          hint: f.hint,
          numeric: f.type === 'number',
          text,
          overridden: !!(user && f.key in user),
          invalid: !!invalidKeys[f.key],
          overriddenLabel: L.overridden,
          resetLabel: L.reset,
          invalidLabel: L.invalidNumber,
          disabled,
          onEdit: (t) => { editField(f.key, t) },
          onReset: () => { clearField(f.key) },
        })
      }

      // Group fields in GROUP_ORDER (unknown groups after), render one titled section per group.
      const byGroup = {}
      for (const f of fields) (byGroup[f.group] || (byGroup[f.group] = [])).push(f)
      const orderedGroups = GROUP_ORDER.filter((g) => byGroup[g]).concat(Object.keys(byGroup).filter((g) => !GROUP_ORDER.includes(g)))
      const sections = orderedGroups.map((g) =>
        h('div', { key: 'grp-' + g, style: { marginTop: 14 } },
          h('div', { style: { fontWeight: 600, fontSize: 13, opacity: 0.8, borderBottom: '1px solid rgba(127,127,127,.25)', paddingBottom: 4, marginBottom: 6 } }, g),
          h('div', { style: { display: 'flex', flexDirection: 'column', gap: 4 } }, byGroup[g].map(renderField))))

      return h(SettingsForm, { labels, state: formState, onSave: () => { save() }, onDiscard: () => { setFailed(false); setErrMsg(''); setDrafts({}) } },
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: 2 } },
          sections,
          errMsg ? h('pre', { style: { color: '#c00', whiteSpace: 'pre-wrap', fontSize: 12, marginTop: 8 } }, '错误：' + errMsg) : null))
    }

    /** Slot component factory: renders the boundary-wrapped settings form. */
    function makeCard(ctx) {
      const cf = ctx.configForms
      return function HitMindCard(props) {
        if (props.view === 'summary') return L.summary
        let form, describe
        try {
          form = cf.get(NS)
          describe = cf.describe()
        } catch (err) {
          return h('pre', { style: { color: '#c00', whiteSpace: 'pre-wrap', fontSize: 12 } }, L.renderError + String(err))
        }
        if (!form || !describe) return h('p', { style: { opacity: 0.7 } }, L.noService)
        return h(Boundary, null, h(HitMindCardBody, { view: props.view, form, describe }))
      }
    }

    /** Required client services (cordis fiber inject). */
    exports.inject = ['slots', 'configForms']

    /**
     * Show the configuration on THIS bundle's own page (the page opened by clicking
     * the installed `dsh-hitmind` plugin), not in the "built-in plugins" list.
     * `plugins.bundle.config` is a keyed slot matched by the bundle package name;
     * registering under `dsh-hitmind` makes the page render our form (the page
     * gates that section on `ledger.bundles.has(pkg.name)`). We also require the
     * Host to serve the `hitmind` settings namespace so the card never appears
     * without live config.
     * @param ctx - the browser plugin context.
     */
    exports.apply = function apply(ctx) {
      try {
        const Card = makeCard(ctx)
        let off
        const sync = () => {
          const view = ctx.configForms.describe().getSnapshot().view
          const served = !!view && view.namespaces.some((n) => n.ns === NS)
          if (served && !off) {
            off = ctx.slots.inject('plugins.bundle.config', () => ctx.slots.register({
              name: 'plugins.bundle.config',
              key: PKG,
            }, Card))
          } else if (!served && off) {
            off(); off = undefined
          }
        }
        ctx.effect(() => ctx.configForms.describe().subscribe(sync), 'dsh-hitmind: bundle config watch')
        ctx.effect(() => () => { if (off) { off(); off = undefined } }, 'dsh-hitmind: bundle config disposer')
        sync()
      } catch (err) {
        try { console.error('[hitmind] settings card registration failed', err) } catch (e) {}
        throw err
      }
    }

    return module.exports
  },
})
