"""Single source of truth for HitMind's tunable settings (core-owned, zero-dependency).

Every configurable knob is declared ONCE here — its config key, ``HITMIND_*`` env
name, type, default, grouping, and how it maps onto ``Engine`` — so the Hermes
config panel (``hitmind/config_schema.py``), the Hermes adapter, and the DSH daemon
all read the same facts instead of re-declaring defaults in three places.

This module imports only the Python stdlib and must stay host-agnostic: it is
imported by core/daemon without Hermes present. Host layers translate it into their
own UI/schema types (Hermes -> ProviderField, DSH -> Schemastery via generated JS).

Design note (raw_ttl default): the *host* default for ``raw_ttl_days`` is 30 days.
``Engine``'s bare fallback (no host wiring, e.g. benchmark corpus ingestion) keeps
"disabled" — that is intentional and not a drift: hosts always pass the spec default
explicitly; benchmarks pass ``raw_ttl_days=0``/None to keep every turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# Schema/renderer kinds (string tokens; host layers map to their own constants).
K_TEXT = "text"
K_NUMBER = "number"
K_BOOL = "bool"
K_SELECT = "select"
K_SECRET = "secret"

# Group labels (shared by the Hermes panel grouping and DSH description).
G_EXTRACT = "记忆抽取"
G_RECALL = "召回"
G_IMPORTANCE = "重要性"
G_TOPIC = "Topic 聚类"
G_LIFE = "生命周期管理"
G_MODEL = "模型配置"
G_LOG = "日志"


@dataclass(frozen=True)
class Setting:
    """One tunable's declarative facts (the single source of truth)."""

    key: str                       # config.json / schema key
    env: str                       # HITMIND_* env name (also Engine's env fallback)
    kind: str                      # K_TEXT | K_NUMBER | K_BOOL | K_SELECT | K_SECRET
    default: Any                   # native Python default
    label: str
    description: str = ""
    group: str = ""
    # How this maps onto Engine(**kwargs); exactly one of engine_arg / engine_dict is set.
    engine_arg: str | None = None            # scalar Engine kwarg name
    engine_dict: str | None = None           # one of 'kind_importance'|'half_life_days'|'ttl_days'
    dict_key: str | None = None              # sub-key inside engine_dict
    cast: str = "str"                        # 'str'|'int'|'float'|'bool' (native conversion)
    options: tuple[tuple[str, str], ...] = ()  # select: (value, label)
    inline: bool = False
    secret: bool = False


# --------------------------------------------------------------------------- #
# The catalog. Order mirrors the Hermes panel (记忆抽取/召回/重要性/Topic/生命周期/模型).
# --------------------------------------------------------------------------- #
SETTINGS: tuple[Setting, ...] = (
    # ============================ 记忆抽取 ============================ #
    Setting("sync_roles", "HITMIND_SYNC_ROLES", K_SELECT, "user", "自动捕获角色",
            "哪些说话角色会被自动捕获为记忆。", G_EXTRACT,
            options=(("user", "仅 user"), ("user,assistant", "user + assistant"))),
    Setting("sync_user_min", "HITMIND_SYNC_USER_MIN", K_NUMBER, 0,
            "user 消息最小长度（字符）", "短于此长度的 user 消息不捕获；0=不限制（默认）。", G_EXTRACT, cast="int"),
    Setting("sync_assistant_min", "HITMIND_SYNC_ASSISTANT_MIN", K_NUMBER, 10,
            "assistant 消息最小长度（字符）", "短于此长度的 assistant 消息不捕获。", G_EXTRACT, cast="int"),
    Setting("consolidate_every_turns", "HITMIND_SYNC_CONSOLIDATE_EVERY", K_NUMBER, 10,
            "chat 批次轮数阈值", "未处理的真实对话回合数（user 消息数）≥ 2×此值 时标记待处理批次（闲时抽取）。", G_EXTRACT,
            engine_arg="consolidate_every_turns", cast="int"),
    Setting("memory_max_chars", "HITMIND_MEMORY_MAX_CHARS", K_NUMBER, 1024,
            "单条记忆最大长度（字符）",
            "每条记忆内容的最大字符数；该限制会动态写入 LLM 提示词，提示超出时拆分为多条记忆（超出硬上限仍会截断；0=不限）。",
            G_EXTRACT, engine_arg="memory_max_chars", cast="int"),
    Setting("time_normalize", "HITMIND_TIME_NORMALIZE", K_BOOL, True,
            "时间归一化（相对日期→绝对日期）",
            "写入时把“昨天/下周三”等按逻辑时间补成绝对日期（原文保留，另存 normalized_text 参与检索并随结果返回）；确定性、零 LLM。",
            G_EXTRACT, engine_arg="time_normalize", cast="bool"),
    Setting("tz_hours", "HITMIND_TZ_HOURS", K_TEXT, "auto",
            "时间戳时区（auto=跟随系统，或填小时偏移）",
            "写入记忆时间戳所用的本地时区偏移。默认 auto=自动读取系统时区（零配置）；也可手填覆盖：整数/小数小时（8、5.5、-3.5）或时:分（5:30、+05:45、-03:30），支持半小时/刻钟时区。",
            G_EXTRACT, engine_arg="tz_hours"),
    Setting("skip_contexts", "HITMIND_SKIP_CONTEXTS", K_TEXT,
            "cron,subagent,flush,background,skill_loop", "跳过捕获的上下文",
            "逗号分隔；这些上下文中的消息不会被自动捕获。", G_EXTRACT),
    Setting("work_platforms", "HITMIND_WORK_PLATFORMS", K_TEXT, "desktop,cli,tui,local",
            "work 模式平台", "逗号分隔；这些平台按 work 模式（压缩驱动）处理，其余按 chat 模式。", G_EXTRACT),
    Setting("chat_cooldown_seconds", "HITMIND_CHAT_COOLDOWN_SECONDS", K_NUMBER, 1200,
            "失败冷却（秒）", "chat 批次连续抽取失败后冷却多久再重试（保留消息，绝不丢）。", G_EXTRACT,
            engine_arg="chat_cooldown_seconds", cast="int"),
    Setting("idle_consolidate_seconds", "HITMIND_IDLE_CONSOLIDATE_SECONDS", K_NUMBER, 300,
            "闲时阈值（秒）", "会话空闲 ≥ 此秒数才处理后台任务；0 关闭闲时整理。", G_EXTRACT,
            engine_arg="idle_consolidate_seconds", cast="int"),
    Setting("idle_poll_seconds", "HITMIND_IDLE_POLL_SECONDS", K_NUMBER, 60,
            "后台轮询间隔（秒）", "闲时后台线程每隔多少秒检查一次是否有待办（最小 15）。", G_EXTRACT,
            engine_arg="idle_poll_seconds", cast="int"),

    # ============================ 召回 ============================ #
    Setting("recall_top_k", "HITMIND_RECALL_TOP_K", K_NUMBER, 5, "召回条数（top_k）",
            "每轮注入/查询返回的最大命中条数。", G_RECALL, engine_arg="recall_top_k", cast="int"),
    Setting("dense_floor", "HITMIND_DENSE_FLOOR", K_NUMBER, 0.5, "向量最低相似度（余弦）",
            "dense 检索的余弦门槛：低于此值的候选在融合前被丢弃（绝对余弦，非相对分）。不同 embedding 模型分布不同，需据此调整；0.5≈embeddinggemma 无关文本底噪。",
            G_RECALL, engine_arg="dense_floor", cast="float"),
    Setting("topic_expand_cap", "HITMIND_TOPIC_EXPAND_CAP", K_NUMBER, 3,
            "同主题扩展：每主题总共保留条数",
            "每个被展开的 topic 最多保留的总条数（已命中的那条也算在内，最新优先）；去重后不补足。", G_RECALL,
            engine_arg="topic_expand_cap", cast="int"),
    Setting("topic_expand_hits", "HITMIND_TOPIC_EXPAND_HITS", K_NUMBER, 3,
            "同主题扩展：取前 N 条命中的 topic",
            "固定对 RRF 融合结果前 N 条记忆所在的 topic 做扩展（无论这几条是否同主题）。", G_RECALL,
            engine_arg="topic_expand_hits", cast="int"),
    Setting("rel_weight", "HITMIND_REL_WEIGHT", K_NUMBER, 0.8, "相关性权重（0–1）",
            "最终得分 = 相关性权重×相关度 + 重要性权重×重要性 + (余下)×新鲜度。", G_RECALL,
            engine_arg="rel_weight", cast="float"),
    Setting("importance_weight", "HITMIND_IMPORTANCE_WEIGHT", K_NUMBER, 0.1, "重要性权重（0–1）",
            "最终得分中重要性的权重（默认 0.8/0.1/0.1 = 相关度/重要性/新鲜度）。", G_RECALL,
            engine_arg="importance_weight", cast="float"),

    # ============================ 重要性 ============================ #
    Setting("importance_persona", "HITMIND_IMPORTANCE_PERSONA", K_NUMBER, 1.0,
            "persona 重要性（0–1）", "persona 类记忆写入时的默认重要性。", G_IMPORTANCE,
            engine_dict="kind_importance", dict_key="persona", cast="float"),
    Setting("importance_fact", "HITMIND_IMPORTANCE_FACT", K_NUMBER, 1.0,
            "fact 重要性（0–1）", "fact 类记忆写入时的默认重要性。", G_IMPORTANCE,
            engine_dict="kind_importance", dict_key="fact", cast="float"),
    Setting("importance_event", "HITMIND_IMPORTANCE_EVENT", K_NUMBER, 1.0,
            "event 重要性（0–1）", "event 类记忆写入时的默认重要性。", G_IMPORTANCE,
            engine_dict="kind_importance", dict_key="event", cast="float"),
    Setting("importance_procedure", "HITMIND_IMPORTANCE_PROCEDURE", K_NUMBER, 1.0,
            "procedure 重要性（0–1）", "procedure 类记忆写入时的默认重要性。", G_IMPORTANCE,
            engine_dict="kind_importance", dict_key="procedure", cast="float"),

    # ============================ Topic 聚类 ============================ #
    Setting("topic_min_cluster", "HITMIND_TOPIC_MIN_CLUSTER", K_NUMBER, 2, "每簇够格线（条）",
            "一个簇成员 ≥ 此值才送 LLM 判定是否同一件事（单条不聚合）。", G_TOPIC,
            engine_arg="topic_min_cluster", cast="int", inline=True),
    Setting("cluster_min_size", "HITMIND_CLUSTER_MIN_SIZE", K_NUMBER, 10, "触发积压门槛（条）",
            "全部待整理(pre_clustered)成员数合计 ≥ 此值才启动一轮 topic 整理。", G_TOPIC,
            engine_arg="cluster_min_size", cast="int", inline=True),
    Setting("consolidate_min_working", "HITMIND_CONSOLIDATE_MIN_WORKING", K_NUMBER, 50,
            "定稿最小待办数（条）",
            "单轮 topic 定稿前，待处理(pending)记忆数 ≥ 此值才值得启动（低于则跳过，省 LLM）。", G_TOPIC,
            engine_arg="consolidate_min_working", cast="int"),
    Setting("cluster_cos_floor", "HITMIND_CLUSTER_COS_FLOOR", K_NUMBER, 0.78, "聚类余弦门槛（0–1）",
            "两条记忆并入同簇的最小余弦相似度。不同 embedding 模型的相似度分布不同，需据此调整（不同模型维度/训练目标不同，门槛差异较大）。",
            G_TOPIC, engine_arg="cluster_cos_floor", cast="float"),
    Setting("topic_max_members", "HITMIND_TOPIC_MAX_MEMBERS", K_NUMBER, 10, "单 topic 最大记忆条数",
            "一个 topic 簇最多容纳的成员数，超过则另起新簇。", G_TOPIC,
            engine_arg="topic_max_members", cast="int"),
    Setting("cluster_scan_window", "HITMIND_CLUSTER_SCAN_WINDOW", K_NUMBER, 200, "预聚类比较窗口（条）",
            "新记忆只与最近 N 条已有记忆比较以决定归簇。", G_TOPIC,
            engine_arg="cluster_scan_window", cast="int"),

    # ============================ 生命周期管理 ============================ #
    Setting("episode_ttl_days", "HITMIND_EPISODE_TTL_DAYS", K_NUMBER, 90, "episode 保留天数",
            "压缩摘要 episode 保留天数，到期物理删除。", G_LIFE,
            engine_arg="episode_ttl_days", cast="int"),
    Setting("raw_ttl_days", "HITMIND_RAW_TTL_DAYS", K_NUMBER, 30, "原始捕获保留天数",
            "未整理的逐轮原始记忆保留天数（0=不清理）。", G_LIFE,
            engine_arg="raw_ttl_days", cast="int"),
    Setting("capacity_warn", "HITMIND_CAPACITY_WARN", K_NUMBER, 200000, "容量告警阈值（条）",
            "active 记忆超过此值仅告警，不删除。", G_LIFE,
            engine_arg="capacity_warn", cast="int"),
    Setting("version_keep", "HITMIND_VERSION_KEEP", K_NUMBER, 5, "记忆保留历史版本数",
            "一条记忆被多次更新时，保留多少条旧版本（更早的标记删除）。", G_LIFE,
            engine_arg="version_keep", cast="int"),
    Setting("review_trigger_min", "HITMIND_REVIEW_TRIGGER_MIN", K_NUMBER, 3, "待复核触发阈值（条）",
            "被判为待复核(inactive)的记忆超过此条数，闲时用 LLM 判定是否同一件事并更新版本。", G_LIFE,
            engine_arg="review_trigger_min", cast="int"),
    Setting("ttl_event_days", "HITMIND_TTL_EVENT_DAYS", K_NUMBER, 90, "event 过期天数",
            "event 类记忆的默认过期天数。", G_LIFE,
            engine_dict="ttl_days", dict_key="event", cast="int"),
    Setting("ttl_persona_days", "HITMIND_TTL_PERSONA_DAYS", K_NUMBER, 0, "persona 过期天数（0=永不过期）",
            "persona 类默认过期天数；0 表示不按时间过期。", G_LIFE,
            engine_dict="ttl_days", dict_key="persona", cast="int"),
    Setting("ttl_fact_days", "HITMIND_TTL_FACT_DAYS", K_NUMBER, 0, "fact 过期天数（0=永不过期）",
            "fact 类默认过期天数；0 表示不按时间过期。", G_LIFE,
            engine_dict="ttl_days", dict_key="fact", cast="int"),
    Setting("ttl_procedure_days", "HITMIND_TTL_PROCEDURE_DAYS", K_NUMBER, 0,
            "procedure 过期天数（0=永不过期）",
            "procedure 类默认过期天数；0 表示不按时间过期。", G_LIFE,
            engine_dict="ttl_days", dict_key="procedure", cast="int"),
    Setting("half_life_event_days", "HITMIND_HALF_LIFE_EVENT_DAYS", K_NUMBER, 14, "event 半衰期（天）",
            "event 类新鲜度半衰期。", G_LIFE,
            engine_dict="half_life_days", dict_key="event", cast="int"),
    Setting("half_life_persona_days", "HITMIND_HALF_LIFE_PERSONA_DAYS", K_NUMBER, 180,
            "persona 半衰期（天）", "persona 类新鲜度半衰期。", G_LIFE,
            engine_dict="half_life_days", dict_key="persona", cast="int"),
    Setting("half_life_fact_days", "HITMIND_HALF_LIFE_FACT_DAYS", K_NUMBER, 180, "fact 半衰期（天）",
            "fact 类新鲜度半衰期。", G_LIFE,
            engine_dict="half_life_days", dict_key="fact", cast="int"),
    Setting("half_life_procedure_days", "HITMIND_HALF_LIFE_PROCEDURE_DAYS", K_NUMBER, 60,
            "procedure 半衰期（天）", "procedure 类新鲜度半衰期。", G_LIFE,
            engine_dict="half_life_days", dict_key="procedure", cast="int"),

    # ============================ 模型配置 ============================ #
    Setting("embedding_base_url", "HITMIND_EMBED_BASE_URL", K_TEXT, "",
            "Embedding 服务地址（/v1）",
            "如 http://127.0.0.1:8091/v1；留空则退化为纯 BM25。", G_MODEL),
    Setting("embedding_model", "HITMIND_EMBED_MODEL", K_TEXT, "embeddinggemma-2",
            "Embedding 模型名", "本地 embedding 服务的模型名。", G_MODEL),
    Setting("embedding_api_key", "HITMIND_EMBED_API_KEY", K_SECRET, "",
            "Embedding API Key",
            "本地服务通常留空；保存在 ~/.hermes/.env，不写入 config.json。", G_MODEL,
            secret=True),
    Setting("rerank_enabled", "HITMIND_RERANK_ENABLED", K_BOOL, True,
            "启用重排（cross-encoder）",
            "对召回短名单用本地 rerank 模型重排后再取 top_k。模型不可用时自动跳过（fail-open）。",
            G_MODEL, engine_arg="rerank_enabled", cast="bool"),
    Setting("rerank_model", "HITMIND_RERANK_MODEL", K_TEXT,
            "temsa/mmarco-mMiniLMv2-L12-H384-v1-onnx-cpu-qint8", "重排模型 id",
            "本地 /rerank 端点（与 embedding 同一 base_url）使用的 cross-encoder 模型 id。", G_MODEL),
    Setting("rerank_pool", "HITMIND_RERANK_POOL", K_NUMBER, 30, "重排候选池（条）",
            "取前多少条一阶段候选交给 rerank 重排（越大越准越慢）。", G_MODEL,
            engine_arg="rerank_pool", cast="int"),
    Setting("llm_timeout_s", "HITMIND_LLM_TIMEOUT_S", K_NUMBER, 90, "LLM 调用超时（秒）",
            "单次宿主 LLM 调用的硬超时，防止挂死。", G_MODEL, cast="int"),
    Setting("consolidate_llm_enabled", "HITMIND_CONSOLIDATE_LLM_ENABLED", K_BOOL, True,
            "启用 LLM 定稿", "关闭则只做零 LLM 预聚类，不做 topic 定稿（省 LLM）。", G_MODEL,
            engine_arg="consolidate_llm_enabled", cast="bool"),
    Setting("bg_runner", "HITMIND_BG_RUNNER", K_BOOL, True, "启用闲时后台线程",
            "关闭后 chat 批次与 topic 整理不会自动运行。", G_MODEL, cast="bool"),

    # ============================ 日志 ============================ #
    Setting("log_level", "HITMIND_LOG_LEVEL", K_SELECT, "INFO", "日志级别",
            "DEBUG/INFO/WARNING/ERROR/CRITICAL。INFO 只记录必要事件（关键写/召回、"
            "后台任务结果、错误）；DEBUG 用于细粒度排查。绝不记录记忆正文/对话/摘要/Prompt/密钥。",
            G_LOG, options=(("DEBUG", "DEBUG"), ("INFO", "INFO"),
                            ("WARNING", "WARNING"), ("ERROR", "ERROR"), ("CRITICAL", "CRITICAL"))),
    Setting("log_file", "HITMIND_LOG_FILE", K_TEXT, "auto", "日志文件位置",
            "auto=写到数据库同级的 logs/ 目录并按大小轮转；填 stderr=不落盘只输出到 stderr；"
            "或填绝对/相对文件路径。接入宿主日志系统时另镜像一份到该文件。", G_LOG),
)

# Fast lookups.
BY_KEY: dict[str, Setting] = {s.key: s for s in SETTINGS}


def _cast(value: Any, kind: str) -> Any:
    """Coerce a raw value to the setting's native type, tolerating bad input."""
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        return None


def _tz_value(raw: Any) -> float | None:
    """tz_hours: 'auto'/''/None -> None (read system tz); else float hours."""
    if raw is None:
        return None
    s = str(raw).strip().lower()
    if s in ("", "auto"):
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def env_map() -> dict[str, str]:
    """config key -> HITMIND_* env name (for env-only hosts, e.g. the DSH daemon)."""
    return {s.key: s.env for s in SETTINGS}


def defaults() -> dict[str, Any]:
    """config key -> native default."""
    return {s.key: s.default for s in SETTINGS}


def build_engine_kwargs(resolve: Callable[[str], Any]) -> dict[str, Any]:
    """Build the ``Engine(**kwargs)`` tunables from any value resolver.

    ``resolve(key) -> raw value`` returns the effective value for a config key
    (host layers implement config.json > env > default; callers may ignore ``key``
    and read env directly). Returns only the tunable kwargs whose mapping is declared
    in the spec — dict groups (kind_importance/half_life_days/ttl_days) are assembled
    as whole dicts. Callers add db_path/agent_id/embedder/reranker/retrieval_config.
    """
    kwargs: dict[str, Any] = {}
    dicts: dict[str, dict[str, Any]] = {"kind_importance": {}, "half_life_days": {}, "ttl_days": {}}
    for s in SETTINGS:
        raw = resolve(s.key)
        if raw is None:
            raw = s.default
        if s.engine_dict is not None:
            v = _cast(raw, s.cast)
            if v is None:
                v = s.default
            if s.engine_dict == "ttl_days":
                # ttl: 0/None means "never expires" -> None
                dicts["ttl_days"][s.dict_key] = v if v and v > 0 else None
            else:
                dicts[s.engine_dict][s.dict_key] = v
        elif s.engine_arg is not None:
            if s.engine_arg == "tz_hours":
                kwargs["tz_hours"] = _tz_value(raw)
            else:
                v = _cast(raw, s.cast)
                kwargs[s.engine_arg] = s.default if v is None else v
    for name, d in dicts.items():
        if d:
            kwargs[name] = d
    return kwargs
