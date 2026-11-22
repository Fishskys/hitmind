"""Hermes MemoryProvider binding for HitMind.

Maps the Hermes lifecycle onto the host-agnostic core Engine:
- initialize() binds db_path from hermes_home (never hardcode ~/.hermes)
- prefetch()  -> core.recall() -> memory excerpts block `[id] (kind)[@date]：content` (bounded)
- sync_turn() -> auto per-turn capture (mirrors Mnemosyne: filter machine envelopes,
  store user/optional assistant past length thresholds, topic-tagged)
- get_tool_schemas()/handle_tool_call() -> expose the 9 memory verbs

The provider is duck-typed; if Hermes' MemoryProvider ABC isn't importable we fall
back to ``object`` so the module still imports in isolation.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

from ...core import models
from ...core.engine import Engine, make_embedder_from_env
from ...tools import _TOOL_MAP, _TOOL_SCHEMAS  # host-agnostic single source of truth
from .envelope import sanitize_user_content
from .llm import HostClusterLLM
from .tasks import BackgroundTaskRunner

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depends on host
    from agent.memory_provider import MemoryProvider  # type: ignore
except Exception:  # pragma: no cover
    MemoryProvider = object  # type: ignore


# --- Host registry registration (progressive tool disclosure) -----------------
# When the host exposes ``ctx.register_tool`` (Hermes), register EVERY tool into the host
# tool registry under toolset ``"hitmind"``. Registered non-core plugin tools are then
# *deferrable*: progressive disclosure (``tools/tool_search.py``) moves them behind the
# ``tool_search``/``tool_describe``/``tool_call`` bridge and loads a tool's schema only when
# the model asks for it — so the 9+ schemas no longer sit in every prompt. Dispatch is
# engine-agnostic: the registry handler resolves the live Engine from the currently-active
# provider instances, so it keeps working even though the host constructs a fresh provider
# per message. When ``ctx.register_tool`` is unavailable we fall back to ``get_tool_schemas``.
_ACTIVE_PROVIDERS: list = []


def _register_dispatch(name: str, args: dict) -> str:
    """Dispatch a registry-invoked tool call to the active provider's live Engine."""
    for prov in list(_ACTIVE_PROVIDERS):
        if getattr(prov, "_engine", None) is not None:
            return prov.handle_tool_call(name, args or {})
    return json.dumps(models.err(models.INTERNAL, "hitmind not initialized"))


def _register_tools_with_host(ctx) -> bool:
    """Register all HitMind tools in the host registry under toolset 'hitmind'.

    Returns True when the host supports ``ctx.register_tool`` and registration was attempted
    (so the provider can suppress its legacy ``get_tool_schemas`` exposure)."""
    register_tool = getattr(ctx, "register_tool", None)
    if not callable(register_tool):
        return False
    schemas = list(_TOOL_SCHEMAS)
    for schema in schemas:
        nm = schema.get("name")
        if not nm:
            continue

        def _handler(args, _name=nm, **kw):
            return _register_dispatch(_name, args)

        try:
            register_tool(name=nm, toolset="hitmind", schema=schema,
                          handler=_handler, description=schema.get("description", ""))
        except Exception as exc:  # a secondary registration must not break the provider
            logger.debug("hitmind register_tool(%s) skipped: %s", nm, exc)
    return True


class HitMindMemoryProvider(MemoryProvider):  # type: ignore[misc]
    def __init__(self) -> None:
        self._engine: Engine | None = None
        self._session_id = ""
        self._agent_context = "primary"
        self._mode = "chat"
        self._sync_lock = threading.Lock()
        self._turn_count = 0
        # Policy fields are resolved in initialize() (desktop config -> env -> default);
        # these are just placeholders so the object is usable before init.
        self._skip_contexts: set[str] = set()
        self._sync_roles: set[str] = {"user"}
        self._user_min_len = 0
        self._assistant_min_len = 10
        # actually taken from the Engine (consolidate_every_turns) after initialize()
        self._sync_consolidate_every = 10
        # idle/background runner (chat batch + topic aggregation); resolved in initialize()
        self._auto_runner = True
        self._runner: BackgroundTaskRunner | None = None
        # set True when tools are registered in the host registry (registration time); in that
        # mode we do NOT also expose them via get_tool_schemas (avoids double tool surface).
        self._registry_mode = False

    @property
    def name(self) -> str:
        return "hitmind"

    def is_available(self) -> bool:
        try:
            from ...core.engine import Engine as _E  # noqa: F401
            return True
        except Exception:
            return False

    def _config_value(self, key: str, env: str, default, cast=str):
        """Resolve a tunable, precedence: Desktop config (``$HERMES_HOME/hitmind/config.json``,
        written by the memory-provider config panel) FIRST, then the ``HITMIND_*`` env var, then
        the built-in default. Type-cast defensively: a bad value logs a warning and falls through
        to the next source (never crashes init)."""
        native = getattr(self, "_native_cfg", None) or {}
        raw = native.get(key)
        got = raw is not None and raw != ""
        if not got:
            raw = os.environ.get(env)
            got = raw is not None
        if not got:
            return default
        try:
            return cast(raw)
        except (TypeError, ValueError):
            logger.warning("hitmind: bad value for %s=%r; using default %r", key, raw, default)
            return default

    @staticmethod
    def _read_native_config(hermes_home: str) -> dict:
        """Load ``$HERMES_HOME/hitmind/config.json`` (flat_json store for the provider's
        Desktop config panel). Missing/malformed -> {} (env/defaults then apply)."""
        import json
        path = Path(hermes_home) / "hitmind" / "config.json"
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                return data if isinstance(data, dict) else {}
        except Exception as exc:  # pragma: no cover
            logger.debug("hitmind: cannot read %s: %s", path, exc)
        return {}

    def _config_bool(self, key: str, env: str, default: bool) -> bool:
        def _b(v):
            if isinstance(v, bool):
                return v
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        return self._config_value(key, env, default, cast=_b)

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        hermes_home = kwargs.get("hermes_home") or os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
        self._session_id = session_id
        self._agent_context = kwargs.get("agent_context", "primary")
        # Desktop config-panel values (flat_json at $HERMES_HOME/hitmind/config.json); highest
        # precedence over HITMIND_* env. Loaded once per provider instance (per message).
        self._native_cfg = self._read_native_config(hermes_home)
        db_path = Path(hermes_home) / "hitmind" / "data" / "hitmind.db"
        # ---- resolve tunables: desktop config (config_schema) -> HITMIND_* env -> default ----
        # embedding: desktop config wins (embedding_base_url/model), secret api_key stays in .env;
        # make_embedder_from_env() reads HITMIND_EMBED_* (env/.env) as the fallback.
        base_url = self._config_value("embedding_base_url", "HITMIND_EMBED_BASE_URL", "")
        if base_url:
            from ...core.embedder import HttpEmbedder
            embedder = HttpEmbedder(
                str(base_url),
                str(self._config_value("embedding_model", "HITMIND_EMBED_MODEL", "embeddinggemma-2")),
                os.environ.get("HITMIND_EMBED_API_KEY", ""),
            )
        else:
            embedder = make_embedder_from_env()
        # reranker: same local-embed base_url as embedding (the /rerank route sits at the
        # server root). Built only when enabled + a model id is set; otherwise NullReranker.
        _rerank_enabled = self._config_bool("rerank_enabled", "HITMIND_RERANK_ENABLED", True)
        _rerank_model = self._config_value("rerank_model", "HITMIND_RERANK_MODEL",
                                           "temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8")
        if _rerank_enabled and _rerank_model and (base_url or os.environ.get("HITMIND_RERANK_BASE_URL")
                                                  or os.environ.get("HITMIND_EMBED_BASE_URL")):
            from ...core.embedder import make_reranker
            reranker = make_reranker(
                str(base_url) if base_url else None,
                str(_rerank_model),
                os.environ.get("HITMIND_RERANK_API_KEY", "") or os.environ.get("HITMIND_EMBED_API_KEY", ""),
            )
        else:
            from ...core.embedder import NullReranker
            reranker = NullReranker()
        # ---- tunables: single source of truth from settings_spec ----
        # Resolver precedence: Desktop config (config.json) -> HITMIND_* env -> spec default.
        from ...settings_spec import BY_KEY as _SPEC_BY_KEY
        from ...settings_spec import build_engine_kwargs
        native_cfg = self._native_cfg or {}

        def _resolve(key: str):
            st = _SPEC_BY_KEY[key]
            raw = native_cfg.get(key)
            if raw is not None and raw != "":
                return raw
            raw = os.environ.get(st.env)
            if raw is not None:
                return raw
            return st.default

        self._engine = Engine(
            str(db_path), agent_id="hermes", embedder=embedder, reranker=reranker,
            **build_engine_kwargs(_resolve),
        )
        # Track this instance as a live provider so registry-registered tool handlers (which are
        # dispatched outside the memory-manager path) can resolve a live Engine. Bounded, newest
        # kept; the host builds a fresh provider per message.
        try:
            _ACTIVE_PROVIDERS.append(self)
            del _ACTIVE_PROVIDERS[:-8]
        except Exception:  # pragma: no cover
            pass
        # inject the LLM hard-timeout resolved from config into the adapter (module default is env-based)
        try:
            from . import llm as _llm_mod
            _llm_mod.HARD_TIMEOUT_S = float(self._config_value("llm_timeout_s", "HITMIND_LLM_TIMEOUT_S", 90, int))
        except Exception:  # pragma: no cover
            pass
        # adapter-level extraction policy (config first, env fallback)
        self._sync_roles = {
            r.strip().lower()
            for r in str(self._config_value("sync_roles", "HITMIND_SYNC_ROLES", "user")).split(",") if r.strip()
        } & {"user", "assistant"}
        self._user_min_len = self._config_value("sync_user_min", "HITMIND_SYNC_USER_MIN", 0, int)
        self._assistant_min_len = self._config_value("sync_assistant_min", "HITMIND_SYNC_ASSISTANT_MIN", 10, int)
        self._skip_contexts = {
            s.strip().lower() for s in str(self._config_value(
                "skip_contexts", "HITMIND_SKIP_CONTEXTS", "cron,subagent,flush,background,skill_loop"
            )).split(",") if s.strip()
        }
        self._auto_runner = self._config_bool("bg_runner", "HITMIND_BG_RUNNER", True)
        # single source of truth for the tuning knobs lives on the Engine
        self._sync_consolidate_every = self._engine.consolidate_every_turns
        # host-owned LLM (ctx.llm) for offline topic finalize — the ONLY LLM path.
        # Hermes passes a _ProviderCollector whose __getattr__ forwards only
        # register_* calls; the real PluginContext (with .llm) is _plugin_context().
        try:
            plugin_ctx = getattr(self, "_plugin_ctx", None)
            if plugin_ctx is not None and hasattr(plugin_ctx, "_plugin_context"):
                plugin_ctx = plugin_ctx._plugin_context()
            plugin_llm = getattr(plugin_ctx, "llm", None) if plugin_ctx is not None else None
        except Exception as exc:  # pragma: no cover
            logger.debug("hitmind: ctx.llm unavailable: %s", exc)
            plugin_llm = None
        self._engine._llm = HostClusterLLM(plugin_llm) if plugin_llm is not None else None
        if self._engine._llm is not None:
            self._engine._llm.memory_max_chars = self._engine.memory_max_chars
        # ---- mode (work vs chat) by platform; chat by default, config-overridable ----
        platform = str(kwargs.get("platform") or os.environ.get("HITMIND_PLATFORM") or "").lower()
        work = {p.strip().lower() for p in os.environ.get(
            "HITMIND_WORK_PLATFORMS", "desktop,cli,tui,local").split(",") if p.strip()}
        self._mode = "work" if platform in work else "chat"
        # ---- L0 read-only source over Hermes state.db (never copied into HitMind) ----
        try:
            from ...core.state_source import StateDBSource
            self._engine.state_source = StateDBSource(Path(hermes_home) / "state.db")
        except Exception as exc:  # pragma: no cover
            logger.debug("hitmind: state.db source unavailable: %s", exc)
        # ---- extensible idle tasks: chat batch + topic aggregation ----
        try:
            self._engine.register_pending_task("chat_batch", self._engine.process_pending)
            self._engine.register_pending_task("topic", self._engine.run_topic_task)
            self._engine.register_pending_task("review", self._engine.run_review_task)
        except Exception as exc:  # pragma: no cover
            logger.debug("hitmind: pending task registration skipped: %s", exc)
        # ---- start the idle/background runner (daemon, off the hot path) ----
        if self._auto_runner and self._engine.idle_consolidate_seconds > 0:
            try:
                self._runner = BackgroundTaskRunner(self._engine, poll_seconds=self._engine.idle_poll_seconds)
                self._runner.start()
            except Exception as exc:  # pragma: no cover
                logger.debug("hitmind: idle runner start skipped: %s", exc)

    def system_prompt_block(self) -> str:
        return (
            "You have HitMind long-term memory. Use hitmind_remember to save durable "
            "persona/project/event/fact units, and hitmind_recall to retrieve relevant "
            "memories. Prefer explicit recall over guessing."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if self._engine is None or not query.strip():
            return ""
        try:
            res = self._engine.recall(query, top_k=self._engine.recall_top_k)
        except Exception as exc:  # fail-open with signal
            logger.warning("hitmind prefetch failed: %s", exc)
            return ""
        items = (res.get("data") or {}).get("results") or []
        lines: list[str] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = m.get("id")
            if not mid:
                continue  # skip malformed rows instead of one bad item killing the batch
            kind = m.get("kind") or ""
            created = (m.get("created_at") or "")[:10]
            content = (m.get("content") or "").replace("\n", " ").strip()
            line = f"[{mid}] ({kind})[@{created}]：{content}".strip()
            lines.append(line)
        # IMPORTANT: the host (agent.memory_context.build_memory_context_block) OWNS the
        # <memory-context> fence + system note and *strips* any fence a provider adds — so
        # returning our own wrapper triggers a per-turn "pre-wrapped context; stripped"
        # warning (pure noise). Return BARE bullet lines; the host wraps them once.
        return "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "",
                  messages: list | None = None, turn_author: dict | None = None) -> None:
        """Per-turn hook — the HOT path. Writes nothing expensive:
          - work mode: do nothing (capture is driven by post_compaction).
          - chat mode: note activity; once ``consolidate_every_turns`` turns have
            accumulated past the cursor, FLAG a pending batch (no LLM here). The idle
            runner does the actual extraction off the hot path; a wake() nudges it.
        The raw conversation stays in Hermes state.db; HitMind never copies it.
        """
        if self._engine is None or self._agent_context in self._skip_contexts:
            return
        try:
            if self._mode != "chat":
                return  # work: capture is compaction-driven via the hook
            from ...capture_policy import should_capture
            ok, _clean = should_capture(
                "user", user_content,
                roles=self._sync_roles, user_min=self._user_min_len,
                assistant_min=self._assistant_min_len,
                context=self._agent_context, skip_contexts=self._skip_contexts,
                sanitizer=sanitize_user_content,
            )
            if not ok:
                return
            sid = session_id or self._session_id
            res = self._engine.note_activity(sid)
            data = res.get("data") or {}
            if (data.get("pending") or data.get("review_due")) and self._runner is not None:
                self._runner.wake()
        except Exception as exc:
            logger.warning("hitmind sync_turn failed: %s", exc)

    def on_session_end(self, messages: list | None = None, **kwargs: Any) -> None:
        """Real session boundary: flush any pending chat batch (process its tail even if
        the session never went idle), then let the runner idle. Best-effort, off-thread."""
        if self._engine is None or self._agent_context in self._skip_contexts or self._mode != "chat":
            return
        sid = self._session_id
        if not sid:
            return

        def _flush():
            try:
                self._engine.flush_session(sid)
            except Exception as exc:
                logger.warning("hitmind session-end flush failed: %s", exc)

        threading.Thread(target=_flush, daemon=True).start()

    def on_post_compaction(self, session_id: str = "", old_session_id: str = "",
                           kind: str | None = None, reason: str | None = None,
                           before_tokens=None, after_tokens=None,
                           messages_removed=None, **kwargs: Any) -> None:
        """Compaction-driven capture: store Hermes' cumulative summary as an episode and
        let the LLM extract (delta) memories. Runs for both work and chat sessions —
        compaction is the universal, non-LLM-authored summary source."""
        if self._engine is None or self._agent_context in self._skip_contexts:
            return
        if kind not in (None, "summarize", "provider_native", "micro_summarize"):
            return  # skip pure tool-result pruning
        sid = session_id or self._session_id
        if not sid:
            return

        def _work():
            try:
                summary = None
                if self._engine.state_source is not None:
                    summary = self._engine.state_source.latest_summary(sid)
                if summary:
                    self._engine.ingest_compaction(sid, summary, mode=self._mode)
            except Exception as exc:
                logger.warning("hitmind compaction capture failed: %s", exc)

        threading.Thread(target=_work, daemon=True).start()

    def get_tool_schemas(self) -> list:
        # In registry mode (Hermes with ctx.register_tool) the tools are registered on the
        # host tool surface under toolset "hitmind" and loaded on demand via progressive
        # disclosure; do NOT also inject them here (avoids a duplicate tool surface).
        if getattr(self, "_registry_mode", False):
            return []
        return list(_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs: Any) -> str:
        if self._engine is None:
            return json.dumps(models.err(models.INTERNAL, "not initialized"))
        fn = _TOOL_MAP.get(tool_name)
        if fn is None:
            return json.dumps(models.err(models.NOT_FOUND, f"unknown tool: {tool_name}"))
        try:
            return json.dumps(fn(self._engine, args or {}), ensure_ascii=False)
        except models.HitMindError as exc:
            return json.dumps(exc.to_envelope(), ensure_ascii=False)
        except Exception as exc:
            return json.dumps(models.err(models.INTERNAL, str(exc)), ensure_ascii=False)

    def shutdown(self) -> None:
        if self._runner is not None:
            try:
                self._runner.stop()
            except Exception:  # pragma: no cover
                pass
            self._runner = None
        if self._engine is not None:
            self._engine.close()
            self._engine = None


def register(ctx) -> None:
    """Hermes memory-loader entry point."""
    try:
        provider = HitMindMemoryProvider()
        provider._plugin_ctx = ctx
        ctx.register_memory_provider(provider)
        # Register ALL tools in the host registry under toolset "hitmind" so progressive tool
        # disclosure can defer them (loaded on demand). When the host lacks ctx.register_tool,
        # fall back to the provider's get_tool_schemas() exposure.
        try:
            if _register_tools_with_host(ctx):
                provider._registry_mode = True
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.debug("hitmind registry tool registration skipped: %s", exc)
        # Register our auxiliary LLM task so the user can pick provider/model for
        # topic finalization in the standard `auxiliary.hitmind_topic` config (and
        # the picker). Unset -> host falls back to the main model automatically.
        try:
            ctx.register_auxiliary_task(
                "hitmind_topic",
                display_name="HitMind 记忆主题聚合",
                description="HitMind clusters memories into topics offline (LLM finalize)",
                inherit_from="compression",
            )
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.debug("hitmind auxiliary task registration skipped: %s", exc)
        # work-mode capture trigger: distill each committed compaction segment.
        try:
            ctx.register_hook("post_compaction", provider.on_post_compaction)
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.debug("hitmind post_compaction hook registration skipped: %s", exc)
        # chat: flush a pending batch at a real session boundary (tail safety).
        try:
            ctx.register_hook("on_session_end", provider.on_session_end)
        except Exception as exc:  # pragma: no cover - non-fatal
            logger.debug("hitmind on_session_end hook registration skipped: %s", exc)
    except Exception as exc:  # pragma: no cover
        logger.warning("hitmind register failed: %s", exc)
