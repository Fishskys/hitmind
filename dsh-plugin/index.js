/**
 * HitMind — native DeepSeek Harness (DSH) plugin.
 *
 * Written to the official plugin API:
 *   - tool / adding-a-tool: `defineTool` from `@deepseek-ai/dsh-tools`, `ctx.tools.register`
 *   - config: Schemastery `Config` from `@deepseek-ai/schemastery`
 *   - framework / events: `agent/pre-step` waterfall + `session/event`
 *   - llm: `ctx.llm.stream` (host-owned model; HitMind never holds a key)
 *
 * The Python core runs as a child process: `python -m hitmind.daemon` (stdio JSONL).
 * Packaged as a bundle (`dsh.bundle.patch`) and installed via `dsh plugin add`.
 */

import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
import { accessSync, constants as fsConstants } from 'node:fs'
import { defineTool } from '@deepseek-ai/dsh-tools'
import Schema from '@deepseek-ai/schemastery'
import { createUserMessage } from '@deepseek-ai/dsh-llm'

/**
 * Resolve the self-contained daemon binary shipped as a per-platform npm runtime
 * package (`dsh-hitmind-runtime-<os>-<arch>`), so the plugin runs with NO Python.
 * Returns the absolute path if the optional runtime package is installed for this
 * platform, else null (caller falls back to the Python interpreter).
 */
function resolveBundledDaemon(logger) {
  const platform = process.platform   // 'linux' | 'darwin' | 'win32'
  const arch = process.arch           // 'x64' | 'arm64'
  const pkg = `dsh-hitmind-runtime-${platform}-${arch}`
  const exe = platform === 'win32' ? 'hitmind-daemon.exe' : 'hitmind-daemon'
  try {
    const require = createRequire(import.meta.url)
    let pkgJsonPath
    try {
      pkgJsonPath = require.resolve(`${pkg}/package.json`)
    } catch (e) {
      return null // optional runtime not installed for this platform
    }
    const binPath = join(dirname(pkgJsonPath), exe)
    accessSync(binPath, fsConstants.X_OK)
    return binPath
  } catch (e) {
    logger?.debug?.(`[hitmind] bundled daemon not usable (${pkg}): ${String(e)}`)
    return null
  }
}

export const name = 'hitmind'
export const inject = ['tools']

/** Plugin configuration (Schemastery). Volatile fields are projected into DSH's
 *  settings form and editable there (saved to the profile patch). This is the SAME
 *  tunable surface as the Hermes memory-provider config panel (`hitmind/config_schema.py`),
 *  so the two host plugins expose identical settings. Values are bridged to the Python
 *  core via `HITMIND_*` env at sidecar spawn (the core reads them in `Engine.__init__`). */
export const Config = Schema.object({
  // -- deployment / sidecar (DSH-side operational) --
  agentId: Schema.string().default('dsh').volatile(),
  injectEnabled: Schema.boolean().default(true).volatile(),
  captureEnabled: Schema.boolean().default(true).volatile(),
  compactionEnabled: Schema.boolean().default(true).volatile(),
  captureFlushMs: Schema.number().default(8000).volatile(),
  diag: Schema.boolean().default(false).volatile(),
  // DSH is single-host, single-mode: one GLOBAL switch (not per-platform like Hermes).
  // false (default) = chat mode: capture every turn + batch-extract. true = work mode:
  // no per-turn capture; memory is derived only from compaction summaries.
  workMode: Schema.boolean().default(false).volatile(),
  // -- 模型 / 嵌入 --
  embeddingBaseUrl: Schema.string().default('').volatile(),
  embeddingModel: Schema.string().default('embeddinggemma-2').volatile(),
  embeddingApiKey: Schema.string().default('').volatile(),
  rerankEnabled: Schema.boolean().default(true).volatile(),
  rerankModel: Schema.string().default('temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8').volatile(),
  rerankPool: Schema.number().default(30).volatile(),
  llmTimeoutS: Schema.number().default(90).volatile(),
  consolidateLlmEnabled: Schema.boolean().default(true).volatile(),
  bgRunner: Schema.boolean().default(true).volatile(),
  // -- 日志 --
  logLevel: Schema.string().default('INFO').volatile(),
  logFile: Schema.string().default('auto').volatile(),
  // -- 记忆抽取 --
  syncRoles: Schema.string().default('user').volatile(),
  syncUserMin: Schema.number().default(0).volatile(),
  syncAssistantMin: Schema.number().default(10).volatile(),
  consolidateEveryTurns: Schema.number().default(10).volatile(),
  memoryMaxChars: Schema.number().default(1024).volatile(),
  timeNormalize: Schema.boolean().default(true).volatile(),
  tzHours: Schema.string().default('auto').volatile(),
  skipContexts: Schema.string().default('cron,subagent,flush,background,skill_loop').volatile(),
  chatCooldownSeconds: Schema.number().default(1200).volatile(),
  idleConsolidateSeconds: Schema.number().default(300).volatile(),
  idlePollSeconds: Schema.number().default(60).volatile(),
  // -- 召回 / 打分 --
  injectTopK: Schema.number().default(5).volatile(),
  denseFloor: Schema.number().default(0.5).volatile(),
  relWeight: Schema.number().default(0.8).volatile(),
  importanceWeight: Schema.number().default(0.1).volatile(),
  topicExpandCap: Schema.number().default(3).volatile(),
  topicExpandHits: Schema.number().default(3).volatile(),
  importancePersona: Schema.number().default(1.0).volatile(),
  importanceFact: Schema.number().default(1.0).volatile(),
  importanceEvent: Schema.number().default(1.0).volatile(),
  importanceProcedure: Schema.number().default(1.0).volatile(),
  // -- Topic 聚类 --
  topicMinCluster: Schema.number().default(2).volatile(),
  clusterMinSize: Schema.number().default(10).volatile(),
  consolidateMinWorking: Schema.number().default(50).volatile(),
  clusterCosFloor: Schema.number().default(0.78).volatile(),
  topicMaxMembers: Schema.number().default(10).volatile(),
  clusterScanWindow: Schema.number().default(200).volatile(),
  // -- 生命周期管理 --
  episodeTtlDays: Schema.number().default(90).volatile(),
  rawTtlDays: Schema.number().default(30).volatile(),
  capacityWarn: Schema.number().default(200000).volatile(),
  versionKeep: Schema.number().default(5).volatile(),
  reviewTriggerMin: Schema.number().default(3).volatile(),
  ttlEventDays: Schema.number().default(90).volatile(),
  ttlPersonaDays: Schema.number().default(0).volatile(),
  ttlFactDays: Schema.number().default(0).volatile(),
  ttlProcedureDays: Schema.number().default(0).volatile(),
  halfLifeEventDays: Schema.number().default(14).volatile(),
  halfLifePersonaDays: Schema.number().default(180).volatile(),
  halfLifeFactDays: Schema.number().default(180).volatile(),
  halfLifeProcedureDays: Schema.number().default(60).volatile(),
})

/** config key -> [HITMIND_* env name, kind, default]. Mirrors the Hermes
 *  config_schema -> env mapping so both hosts feed the identical core tunables. */
const CFG_ENV = [
  ['embeddingBaseUrl', 'HITMIND_EMBED_BASE_URL', 's', ''],
  ['embeddingModel', 'HITMIND_EMBED_MODEL', 's', 'embeddinggemma-2'],
  ['embeddingApiKey', 'HITMIND_EMBED_API_KEY', 's', ''],
  ['rerankEnabled', 'HITMIND_RERANK_ENABLED', 'b', true],
  ['rerankModel', 'HITMIND_RERANK_MODEL', 's', 'temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8'],
  ['rerankPool', 'HITMIND_RERANK_POOL', 'i', 30],
  ['llmTimeoutS', 'HITMIND_LLM_TIMEOUT_S', 'i', 90],
  ['consolidateLlmEnabled', 'HITMIND_CONSOLIDATE_LLM_ENABLED', 'b', true],
  ['bgRunner', 'HITMIND_BG_RUNNER', 'b', true],
  ['logLevel', 'HITMIND_LOG_LEVEL', 's', 'INFO'],
  ['logFile', 'HITMIND_LOG_FILE', 's', 'auto'],
  ['syncRoles', 'HITMIND_SYNC_ROLES', 's', 'user'],
  ['syncUserMin', 'HITMIND_SYNC_USER_MIN', 'i', 0],
  ['syncAssistantMin', 'HITMIND_SYNC_ASSISTANT_MIN', 'i', 10],
  ['consolidateEveryTurns', 'HITMIND_SYNC_CONSOLIDATE_EVERY', 'i', 10],
  ['memoryMaxChars', 'HITMIND_MEMORY_MAX_CHARS', 'i', 1024],
  ['timeNormalize', 'HITMIND_TIME_NORMALIZE', 'b', true],
  ['tzHours', 'HITMIND_TZ_HOURS', 's', 'auto'],
  ['skipContexts', 'HITMIND_SKIP_CONTEXTS', 's', 'cron,subagent,flush,background,skill_loop'],
  ['chatCooldownSeconds', 'HITMIND_CHAT_COOLDOWN_SECONDS', 'i', 1200],
  ['idleConsolidateSeconds', 'HITMIND_IDLE_CONSOLIDATE_SECONDS', 'i', 300],
  ['idlePollSeconds', 'HITMIND_IDLE_POLL_SECONDS', 'i', 60],
  ['injectTopK', 'HITMIND_RECALL_TOP_K', 'i', 5],
  ['denseFloor', 'HITMIND_DENSE_FLOOR', 'f', 0.5],
  ['relWeight', 'HITMIND_REL_WEIGHT', 'f', 0.8],
  ['importanceWeight', 'HITMIND_IMPORTANCE_WEIGHT', 'f', 0.1],
  ['topicExpandCap', 'HITMIND_TOPIC_EXPAND_CAP', 'i', 3],
  ['topicExpandHits', 'HITMIND_TOPIC_EXPAND_HITS', 'i', 3],
  ['importancePersona', 'HITMIND_IMPORTANCE_PERSONA', 'f', 1.0],
  ['importanceFact', 'HITMIND_IMPORTANCE_FACT', 'f', 1.0],
  ['importanceEvent', 'HITMIND_IMPORTANCE_EVENT', 'f', 1.0],
  ['importanceProcedure', 'HITMIND_IMPORTANCE_PROCEDURE', 'f', 1.0],
  ['topicMinCluster', 'HITMIND_TOPIC_MIN_CLUSTER', 'i', 2],
  ['clusterMinSize', 'HITMIND_CLUSTER_MIN_SIZE', 'i', 10],
  ['consolidateMinWorking', 'HITMIND_CONSOLIDATE_MIN_WORKING', 'i', 50],
  ['clusterCosFloor', 'HITMIND_CLUSTER_COS_FLOOR', 'f', 0.78],
  ['topicMaxMembers', 'HITMIND_TOPIC_MAX_MEMBERS', 'i', 10],
  ['clusterScanWindow', 'HITMIND_CLUSTER_SCAN_WINDOW', 'i', 200],
  ['episodeTtlDays', 'HITMIND_EPISODE_TTL_DAYS', 'i', 90],
  ['rawTtlDays', 'HITMIND_RAW_TTL_DAYS', 'i', 30],
  ['capacityWarn', 'HITMIND_CAPACITY_WARN', 'i', 200000],
  ['versionKeep', 'HITMIND_VERSION_KEEP', 'i', 5],
  ['reviewTriggerMin', 'HITMIND_REVIEW_TRIGGER_MIN', 'i', 3],
  ['ttlEventDays', 'HITMIND_TTL_EVENT_DAYS', 'i', 90],
  ['ttlPersonaDays', 'HITMIND_TTL_PERSONA_DAYS', 'i', 0],
  ['ttlFactDays', 'HITMIND_TTL_FACT_DAYS', 'i', 0],
  ['ttlProcedureDays', 'HITMIND_TTL_PROCEDURE_DAYS', 'i', 0],
  ['halfLifeEventDays', 'HITMIND_HALF_LIFE_EVENT_DAYS', 'i', 14],
  ['halfLifePersonaDays', 'HITMIND_HALF_LIFE_PERSONA_DAYS', 'i', 180],
  ['halfLifeFactDays', 'HITMIND_HALF_LIFE_FACT_DAYS', 'i', 180],
  ['halfLifeProcedureDays', 'HITMIND_HALF_LIFE_PROCEDURE_DAYS', 'i', 60],
]

/** Read a config value that may be a plain value (tests / non-volatile) or a volatile
 *  reference exposing `.get()`. */
function rv(x, dflt) {
  if (x === undefined || x === null) return dflt
  return typeof x.get === 'function' ? x.get() : x
}

// --------------------------------------------------------------------------- //
// Capture policy (mirror of hitmind/capture_policy.py): decide if a turn becomes
// a memory candidate. Kept in the plugin so the DSH capture handler filters BEFORE
// spending an IPC round-trip; the daemon also enforces the same policy defensively.
// --------------------------------------------------------------------------- //
const _MACHINE_ONLY = [/^\[CONTEXT (?:COMPACTION|SUMMARY)\]/i, /^<[^>]+>\s*$/]
const _PREFIX_PATTERNS = [/^\[[A-Z][^\]]{0,40}\]\s*/, /^<[A-Za-z][^>]{0,40}>\s*/, /^---+\s*/m]

function sanitizeUserText(text) {
  let t = (text || '').trim()
  if (!t) return ''
  for (const re of _MACHINE_ONLY) if (re.test(t)) return ''
  let changed = true
  while (changed) {
    changed = false
    for (const re of _PREFIX_PATTERNS) {
      const m = t.match(re)
      if (m) { t = t.slice(m[0].length).trim(); changed = true; break }
    }
  }
  return t
}

function parseRoles(raw) {
  const set = new Set(String(raw || 'user').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean))
  const filtered = ['user', 'assistant'].filter((r) => set.has(r))
  return new Set(filtered.length ? filtered : ['user'])
}

function parseContexts(raw) {
  return new Set(String(raw || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean))
}

/** Decide whether to capture a turn -> { ok, text }. Mirrors should_capture() in Python. */
function shouldCapture(role, content, { roles, userMin, assistantMin, context, skipContexts }) {
  role = String(role || '').trim().toLowerCase()
  if (!roles.has(role)) return { ok: false, text: '' }
  if (context != null && skipContexts && skipContexts.has(String(context).trim().toLowerCase())) return { ok: false, text: '' }
  if (role === 'user') {
    const clean = sanitizeUserText(content)
    if (!clean || clean.length <= Number(userMin)) return { ok: false, text: '' }
    return { ok: true, text: clean }
  }
  const clean = String(content || '').trim()
  if (!clean || clean.length <= Number(assistantMin)) return { ok: false, text: '' }
  return { ok: true, text: clean }
}

// --------------------------------------------------------------------------- //
// Sidecar: spawn `python -m hitmind.daemon`, speak JSONL, answer reverse LLM.
// --------------------------------------------------------------------------- //
class Sidecar {
  constructor(config, ctx) {
    this.cfg = config
    this.ctx = ctx
    this.proc = null
    this.ready = false
    this._seq = 0
    this._pending = new Map() // id -> {resolve, reject, timer}
    this._startPromise = null
    this._spawnKey = null // identity of spawn-affecting config; a change triggers restart
  }

  /** Serialize one env value for [key, envName, kind, default]. Booleans -> '1'/'0';
   *  everything else -> trimmed string (blank skipped by the caller). */
  _envValue(key, kind, dflt) {
    const v = rv(this.cfg[key], dflt)
    if (kind === 'b') return (v === false || v === 'false' || v === 0) ? '0' : '1'
    if (v === undefined || v === null) return ''
    return String(v).trim()
  }

  _env() {
    const env = { ...process.env }
    // Bridge every declared core tunable to its HITMIND_* env (single source: CFG_ENV,
    // which mirrors hitmind/settings_spec.py). Blank values are skipped so the core's
    // own default applies; this is what makes the DSH panel actually drive the engine.
    for (const [key, name, kind, dflt] of CFG_ENV) {
      const s = this._envValue(key, kind, dflt)
      if (s !== '') env[name] = s
    }
    return env
  }

  /** Volatile config that only affects spawn (binary/interpreter/db/agent/env). A change
   *  to any of these requires a sidecar restart; other volatile fields are read live. */
  _spawnIdentity() {
    // Every env-bridged field affects the child's environment, so any change to them
    // requires a respawn to take effect. Include all of CFG_ENV + agent id + whether a
    // bundled runtime is present (installing/removing it flips the spawn, below).
    const id = {
      agentId: rv(this.cfg.agentId, 'dsh'),
      bundled: resolveBundledDaemon() ? '1' : '0',
    }
    for (const [key, name, kind, dflt] of CFG_ENV) id[name] = this._envValue(key, kind, dflt)
    return JSON.stringify(id)
  }

  /** Called on loader/volatile-update: respawn when spawn-affecting config changed. */
  async refreshIfNeeded() {
    if (this._spawnKey !== null && this._spawnIdentity() !== this._spawnKey) {
      await this.stop()
      await this.start()
    }
  }

  start() {
    if (this._startPromise) return this._startPromise
    this._startPromise = new Promise((resolve, reject) => {
      const agentId = String(rv(this.cfg.agentId, 'dsh'))
      // Binary is resolved automatically, never configured: bundled runtime package
      // (zero-Python) when present, otherwise the system Python interpreter. The db
      // path is NOT passed — the daemon resolves a stable per-host default.
      const effectiveBin = resolveBundledDaemon(this.ctx.logger)
      this._spawnKey = this._spawnIdentity()
      let cmd, args
      if (effectiveBin) {
        cmd = effectiveBin
        args = ['--agent-id', agentId]
      } else {
        cmd = 'python3'
        args = ['-m', 'hitmind.daemon', '--agent-id', agentId]
      }
      const opts = { env: this._env(), stdio: ['pipe', 'pipe', 'pipe'] }
      let proc
      try { proc = spawn(cmd, args, opts) } catch (err) { reject(err); return }
      this.proc = proc
      proc.on('error', (err) => { try { reject(err) } catch (e) {} })
      // swallow stream errors (e.g. EPIPE when the daemon dies / restarts) so they
      // never surface as an uncaught exception that crashes the host.
      try { proc.stdin.on('error', (err) => this.ctx.logger?.warn?.(`[hitmind] stdin error: ${String(err)}`)) } catch (e) {}
      proc.on('exit', (code, sig) => {
        this.ready = false
        this.proc = null
        this._startPromise = null // allow a later start() to respawn after an unexpected exit
        for (const [, p] of this._pending) { clearTimeout(p.timer); p.reject(new Error(`sidecar exited (${code ?? sig})`)) }
        this._pending.clear()
      })
      createInterface({ input: proc.stdout }).on('line', (line) => this._onLine(line))
      createInterface({ input: proc.stderr }).on('line', (line) => this.ctx.logger?.debug?.(`[hitmind] ${line}`))
      // handshake — write the ping DIRECTLY (calling _request would re-enter start()
      // while this._startPromise is still unset → infinite recursion).
      const id = String(++this._seq)
      const timer = setTimeout(() => { this._pending.delete(id); reject(new Error('hitmind handshake timeout')) }, 8000)
      this._pending.set(id, { resolve: () => { this.ready = true; resolve() }, reject, timer })
      this._writeRaw({ id, method: 'ping', params: {} })
    })
    return this._startPromise
  }

  _writeRaw(obj) {
    const w = this.proc && this.proc.stdin
    if (!w || w.destroyed || w.writableEnded) return
    try { w.write(JSON.stringify(obj) + '\n') }
    catch (err) { this.ctx.logger?.warn?.(`[hitmind] write failed: ${String(err)}`) }
  }

  _onLine(line) {
    if (!line) return
    let msg
    try { msg = JSON.parse(line) } catch { return }
    if (msg.method === 'host_llm_complete') { this._onReverseLLM(msg); return }
    const p = this._pending.get(msg.id)
    if (!p) return
    clearTimeout(p.timer)
    this._pending.delete(msg.id)
    if (msg.error) p.reject(new Error(msg.error.message || String(msg.error)))
    else p.resolve(msg.result)
  }

  async _onReverseLLM(msg) {
    let reply
    try { reply = { id: msg.id, result: await this._llmComplete(msg.params || {}) } }
    catch (err) { reply = { id: msg.id, error: { message: String(err && err.message ? err.message : err) } } }
    this._writeRaw(reply)
  }

  /** Stream the host model (ctx.llm.stream) and assemble text -> parsed JSON. */
  async _llmComplete(params) {
    const instructions = String(params.instructions || '')
    const blocks = Array.isArray(params.input) ? params.input : []
    const userText = blocks.map((b) => (typeof b === 'string' ? b : (b && b.text) || '')).join('\n')
    const sys = instructions + (params.json_mode ? '\n仅输出一个 JSON 对象，不要任何多余文字或代码块。' : '')
    const messages = [
      { role: 'system', content: [{ type: 'text', text: sys }] },
      { role: 'user', content: [{ type: 'text', text: userText }] },
    ]
    let text = ''
    for await (const chunk of this.ctx.llm.stream({ messages })) {
      if (chunk && chunk.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text
    }
    return { parsed: parseJsonLoose(text) }
  }

  request(method, params, timeoutMs) { return this._request(method, params, timeoutMs) }

  _request(method, params, timeoutMs) {
    return this.start().then(() => new Promise((resolve, reject) => {
      const id = String(++this._seq)
      const timer = setTimeout(() => { this._pending.delete(id); reject(new Error(`hitmind rpc ${method} timed out`)) }, timeoutMs || 30000)
      this._pending.set(id, { resolve, reject, timer })
      this._writeRaw({ id, method, params: params || {} })
    }))
  }

  async stop() {
    try { await this._request('shutdown', {}, 3000) } catch { /* ignore */ }
    try { this.proc?.kill('SIGTERM') } catch { /* ignore */ }
    this.proc = null
    this.ready = false
  }
}

export function parseJsonLoose(text) {
  if (!text) return null
  let t = String(text).trim()
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i)
  if (fence) t = fence[1].trim()
  try { return JSON.parse(t) } catch { /* fall through */ }
  const first = t.indexOf('{'); const last = t.lastIndexOf('}')
  if (first >= 0 && last > first) { try { return JSON.parse(t.slice(first, last + 1)) } catch { /* ignore */ } }
  return null
}

export function blocksToText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return ''
  return content.filter((b) => b && b.type === 'text' && typeof b.text === 'string').map((b) => b.text).join('\n')
}

export function latestUserText(messages) {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m && m.role === 'user' && !(m.source && m.source.kind === name)) {
      const t = blocksToText(m.content)
      if (t && t.trim()) return t
    }
  }
  return ''
}

/** Safe short JSON of a value for diagnostics (never throws, bounded length). */
export function safeBrief(v, n = 200) {
  try { const s = JSON.stringify(v); return s === undefined ? String(v) : s.slice(0, n) }
  catch { return String(v).slice(0, n) }
}

// --------------------------------------------------------------------------- //
// Tool definitions via defineTool (documented API). Parameters convert from the
// daemon's JSON Schema into the tool ParameterSchemaSpec DSL.
// --------------------------------------------------------------------------- //
export function makeTools(schemas, sidecar) {
  return schemas.map((s) =>
    defineTool({
      name: s.name,
      description: s.description || s.name,
      parameters: toParamSpec(s.parameters || { type: 'object', properties: {} }),
      output: {
        schema: { type: 'object', additionalProperties: true },
        render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
      },
      async execute(args) {
        return sidecar.request('tool', { name: s.name, args: args || {} }, 60000)
      },
    }),
  )
}

/** JSON Schema (object root) -> ParameterSchemaSpec DSL ({prop: ValueSchemaSpec & required?}). */
export function toParamSpec(js) {
  const props = js.properties || {}
  const required = new Set(js.required || [])
  const out = {}
  for (const [key, spec] of Object.entries(props)) out[key] = toValueSpec(spec, required.has(key))
  return out
}

function toValueSpec(spec, required) {
  const t = spec.type
  let node
  if (t === 'string') node = { type: 'string' }
  else if (t === 'integer') node = { type: 'integer' }
  else if (t === 'number') node = { type: 'number' }
  else if (t === 'boolean') node = { type: 'boolean' }
  else if (t === 'array') node = { type: 'array', items: toValueSpec(spec.items || { type: 'string' }, false) }
  else if (t === 'object') {
    // explicit object node needs additionalProperties; open when no properties declared
    node = spec.properties
      ? { type: 'object', additionalProperties: false, properties: toParamSpec(spec) }
      : { type: 'object', additionalProperties: true }
  } else node = { type: 'json' }
  if (spec.enum) node.enum = spec.enum
  if (spec.description) node.description = spec.description
  if (required) node.required = true
  return node
}

// --------------------------------------------------------------------------- //
// Plugin activation
// --------------------------------------------------------------------------- //
export async function apply(ctx, config) {
  const cfg = config || {}
  const sidecar = new Sidecar(cfg, ctx)

  // 1) tools — fetch schemas from the daemon, register via defineTool.
  try {
    const res = await sidecar.request('get_schemas', {}, 15000)
    const defs = makeTools((res && res.schemas) || [], sidecar)
    for (const d of defs) ctx.tools.register(d)
    ctx.logger?.debug?.(`[hitmind] registered ${defs.length} tools`)
  } catch (err) {
    ctx.logger?.warn?.(`[hitmind] tool registration failed: ${String(err)}`)
  }

  // 1b) diagnostics: log every session/event so we can observe what DSH emits
  //     (especially compaction/start|summary|end and their real data shapes).
  {
    ctx.on('session/event', (session, event) => {
      if (!rv(cfg.diag, false)) return
      try {
        const sid = String(session.id)
        if (event.type === 'compaction/start') {
          console.error(`[hitmind-diag] compaction/start sid=${sid} data=${safeBrief(event.data)}`)
        } else if (event.type === 'compaction/summary') {
          const blocks = Array.isArray(event.data?.summary) ? event.data.summary : null
          const text = blocks ? blocksToText(blocks) : String(event.data?.summary || '')
          console.error(`[hitmind-diag] compaction/summary sid=${sid} summaryIsArray=${!!blocks} blockTypes=${blocks ? blocks.map(b => b && b.type).join(',') : 'n/a'} textLen=${text.length} textHead=${JSON.stringify(text.slice(0, 160))} shadowedRange=${safeBrief(event.data?.shadowedRange)}`)
        } else if (event.type === 'compaction/end') {
          console.error(`[hitmind-diag] compaction/end sid=${sid} data=${safeBrief(event.data)}`)
        } else if (event.type === 'user/message' || event.type === 'assistant/message') {
          const t = blocksToText(event.data?.content)
          if (t && t.trim()) console.error(`[hitmind-diag] ${event.type} sid=${sid} len=${t.length}`)
        }
      } catch (err) {
        console.error(`[hitmind-diag] diag error: ${String(err)}`)
      }
    })
  }

  // 2) auto-inject relevant memories each turn (agent/pre-step waterfall).
  {
    ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      try {
        if (!rv(cfg.injectEnabled, true)) return decision
        if (!decision || decision.kind === 'reject') return decision
        if (payload.step !== 1) return decision
        if (payload.signal && payload.signal.aborted) return decision
        const msgs = decision.messages || []
        if (msgs.some((m) => m && m.source && m.source.kind === name)) return decision
        const query = latestUserText(msgs)
        if (!query) return decision
        const res = await sidecar.request('recall_inject', { query, top_k: rv(cfg.injectTopK, 5) }, 8000)
        const lines = (res && res.lines) || []
        if (!lines.length) return decision
        const text = lines.join('\n')
        const message = createUserMessage({
          content: [{ type: 'text', text }],
          source: { kind: name, form: 'snapshot', sections: [{ name, text }] },
        })
        return { ...decision, messages: [...msgs, message] }
      } catch (err) {
        ctx.logger?.warn?.(`[hitmind] inject failed (fail-open): ${String(err)}`)
        return decision
      }
    }, { prepend: true })
  }

  // 3) capture: append turns + debounce a chat-batch flush (session/event).
  {
    const timers = new Map()
    const flush = async (sid) => {
      timers.delete(sid)
      try {
        await sidecar.request('note_turn', { session_id: sid }, 8000)
        await sidecar.request('run_pending', { task: 'chat_batch' }, 120000)
      } catch (err) {
        ctx.logger?.warn?.(`[hitmind] capture flush failed: ${String(err)}`)
      }
    }
    const schedule = (sid) => {
      const prev = timers.get(sid)
      if (prev) clearTimeout(prev)
      timers.set(sid, setTimeout(() => flush(sid), Math.max(1000, Number(rv(cfg.captureFlushMs, 8000)))))
    }
    // capture-policy inputs, read live from volatile config (change takes effect next turn)
    const capRoles = () => parseRoles(rv(cfg.syncRoles, 'user'))
    const capSkip = () => parseContexts(rv(cfg.skipContexts, ''))
    const capUMin = () => Number(rv(cfg.syncUserMin, 0))
    const capAMin = () => Number(rv(cfg.syncAssistantMin, 10))
    ctx.on('session/event', (session, event) => {
      try {
        if (!rv(cfg.captureEnabled, true)) return
        // work mode: per-turn capture is off; memories come from compaction summaries only.
        if (rv(cfg.workMode, false)) return
        const sid = String(session.id)
        let role = null
        let text = ''
        if (event.type === 'user/message') {
          if (event.data?.source?.kind !== 'user') return
          role = 'user'; text = blocksToText(event.data.content)
        } else if (event.type === 'assistant/message') {
          role = 'assistant'; text = blocksToText(event.data?.content)
        } else {
          return
        }
        if (!text || !text.trim()) return
        // Apply the shared capture policy (role set / min length / machine-envelope strip /
        // skip-context) so sync_roles·sync_user_min·sync_assistant_min·skip_contexts take effect.
        const decision = shouldCapture(role, text, {
          roles: capRoles(),
          userMin: capUMin(),
          assistantMin: capAMin(),
          context: event.data?.context ?? event.data?.agentContext ?? session.context,
          skipContexts: capSkip(),
        })
        if (!decision.ok) return
        sidecar.request('append_turn', { session_id: sid, role, content: decision.text }, 8000)
          .then(() => schedule(sid))
          .catch((err) => ctx.logger?.warn?.(`[hitmind] append_turn failed: ${String(err)}`))
      } catch (err) {
        ctx.logger?.warn?.(`[hitmind] session/event capture error: ${String(err)}`)
      }
    })
    // flush a session's tail at a durability boundary
    ctx.on('session/flush', async (session) => {
      const sid = String(session.id)
      if (timers.has(sid)) await flush(sid)
    })
  }

  // 4) compaction-driven episodes (compaction/summary -> ingest).
  {
    ctx.on('session/event', (session, event) => {
      if (event.type !== 'compaction/summary') return
      if (!rv(cfg.compactionEnabled, true)) return
      try {
        const sid = String(session.id)
        // DSH: event.data.summary is ContentBlock[] (NOT a string). Normalize to text.
        const raw = event.data && event.data.summary
        const summary = Array.isArray(raw) ? blocksToText(raw) : String(raw || event.data?.text || '')
        if (!summary.trim()) return
        const compactionId = String(event.data?.compactionId || '')
        const mode = rv(cfg.workMode, false) ? 'work' : 'chat'
        sidecar.request('ingest_compaction', { session_id: sid, summary, mode, compaction_id: compactionId }, 120000)
          .catch((err) => ctx.logger?.warn?.(`[hitmind] ingest_compaction failed: ${String(err)}`))
      } catch (err) {
        ctx.logger?.warn?.(`[hitmind] compaction capture error: ${String(err)}`)
      }
    })
  }

  // 5) live config: on a volatile-only settings change, DSH updates our Config references
  //    in place and emits `loader/volatile-update`. Respawn the sidecar if a spawn-affecting
  //    field (binary/interpreter/db/agent/embed) changed; other fields are read live at use.
  ctx.on('loader/volatile-update', () => {
    sidecar.refreshIfNeeded().catch((err) => ctx.logger?.warn?.(`[hitmind] volatile refresh failed: ${String(err)}`))
  })

  // stop the sidecar on dispose, and make sure it is up
  if (typeof ctx.effect === 'function') ctx.effect(() => () => { sidecar.stop().catch(() => {}) })
  try { await sidecar.start() } catch (err) { ctx.logger?.warn?.(`[hitmind] sidecar start failed: ${String(err)}`) }
}
