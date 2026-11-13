"""Host-owned LLM wrapper for cluster finalization (Hermes only).

Uses the host's ``ctx.llm`` (``agent.plugin_llm.PluginLlm``) — the ONLY LLM access
surface, so HitMind never touches provider config or API keys. The topic-finalize
call is routed through a plugin-registered auxiliary task ``hitmind_topic``:
the user picks provider/model for that task in the standard ``auxiliary`` config;
if they leave it empty the host falls back to the main model automatically.

Duck-typed on purpose: this module never imports ``agent`` — the provider passes in
the ``ctx.llm`` object at initialize() time.
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any

logger = logging.getLogger(__name__)

TASK_KEY = "hitmind_topic"

# Per-attempt ceiling for a host LLM call. A hung/very-slow host call must NOT block an
# idle pass forever, so each attempt runs on a daemon thread joined with this timeout
# (attempts themselves are already bounded: task-routed then main-model, i.e. <=2).
HARD_TIMEOUT_S = float(os.environ.get("HITMIND_LLM_TIMEOUT_S", "90"))


def _bounded(fn, timeout: float):
    """Run fn() on a daemon thread, returning (result, exc); on timeout -> (None, TimeoutError)."""
    box: dict[str, Any] = {}

    def run():
        try:
            box["res"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["exc"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        return None, TimeoutError(f"hitmind llm call exceeded {timeout}s")
    return box.get("res"), box.get("exc")

_SYS = (
    "你是长期记忆的离线聚合助手。下面给出同一个候选簇里的若干条记忆（按时间先后编号 [0]、[1]…）。"
    "请判断它们是否在讲**同一件事**，并**仔细检查彼此是否矛盾或前后更新**（同一事实/参数/偏好被后来的记忆推翻或修改）。"
    "只输出一个 JSON 对象，不要任何多余文字：\n"
    '{"same_matter": true/false, '
    '"topic": "若同一件事，给一句简短中文主题（含主体+对象，<=30字），否则空串", '
    '"corrections": [{"index": 更正（较新）那条的序号, "corrects": 被它更正的旧条序号, "note": "更正内容简述"}]}'
)

_SEG_SYS = (
    "你是长期记忆的整理器。输入是 Hermes 的一份【压缩摘要】（累积式，覆盖到目前为止的会话）。"
    "你的任务：从中提取【值得长期记住】的记忆点（事实/偏好/事件/做法），宁缺毋滥，可为 0 条。"
    "kind 只能是 persona/fact/event/procedure。不要把临时噪声、工具输出、口头禅当记忆。"
    "只输出 JSON：\n"
    '{"memories":[{"kind":"fact","content":"...","title":"可选短标题"}]}'
)

_DELTA_SYS = (
    "你是长期记忆的整理器。下面是同一会话的【上一版压缩摘要】与【最新压缩摘要】（后者由前者更新而来）。"
    "请只提取【此次相比上一版新增或发生变化】且值得长期记住的信息（事实/偏好/事件/做法）；"
    "上一版已有的信息不要重复提取；没有值得记的就返回空数组。宁缺毋滥。"
    "kind 只能是 persona/fact/event/procedure。只输出 JSON：\n"
    '{"memories":[{"kind":"fact","content":"...","title":"可选短标题"}]}'
)

_MSG_SYS = (
    "你是长期记忆的整理器。下面是一段真实对话（user/assistant 交替，可能含工具调用）。"
    "请从中提取【值得长期记住】的记忆点（可复用的事实/偏好/事件/做法），宁缺毋滥，可为 0 条。"
    "kind 只能是 persona/fact/event/procedure。不要把临时噪声、工具输出、口头禅当记忆。"
    "只输出 JSON：\n"
    '{"memories":[{"kind":"fact","content":"...","title":"可选短标题"}]}'
)

_REVIEW_SYS = (
    "你是长期记忆的复核助手。下面给出【旧记忆】与【新候选】，请判断二者是否在讲**同一件事**。"
    "如果讲同一件事：再判断新候选是**对旧记忆的更新/更正**（应生成新版本取代旧记忆），"
    "还是仅**重复/佐证**旧记忆（无需新版本）。"
    "只输出一个 JSON 对象，不要多余文字：\n"
    '{"same": true/false, "update": true/false}'
)


class HostClusterLLM:
    """Adapter over ``ctx.llm`` (PluginLlm). ``ok`` gates use in run_finalize_pass."""

    def __init__(self, plugin_llm: Any, *, task: str = TASK_KEY) -> None:
        self._llm = plugin_llm
        self._task = task
        self.ok = plugin_llm is not None
        # per-memory length limit surfaced dynamically in the extraction prompts (0=off)
        self.memory_max_chars = 1024

    def _sys(self, base: str) -> str:
        """Append the configurable per-memory length instruction to an extraction prompt."""
        n = int(getattr(self, "memory_max_chars", 0) or 0)
        if n > 0:
            return base + (
                f"\n单条记忆 content 不超过 {n} 个字符；请尽量按照语义单元拆分（一条记忆讲一件事），"
                f"每条记忆尽可能自包含（独立可读、不依赖其它条的上下文），"
                f"不要为凑长度而压缩或省略（超过硬上限的部分仍会被截断）。"
            )
        return base

    def _structured(self, instructions: str, user_text: str) -> dict | None:
        # Host structured completion (JSON mode); task-routed, else main model.
        # Attempts are bounded (<=2) and each is time-boxed by HARD_TIMEOUT_S so a hung
        # host call can never block an idle pass indefinitely.
        attempts = []
        if self._task:
            attempts.append({"task": self._task})
        attempts.append({})
        for extra in attempts:
            res, exc = _bounded(
                lambda extra=extra: self._llm.complete_structured(
                    instructions=instructions,
                    input=[{"type": "text", "text": user_text}],
                    json_mode=True,
                    **extra,
                ),
                HARD_TIMEOUT_S,
            )
            if exc is not None:
                logger.debug("hitmind structured call %r failed: %s", extra.get("task"), exc)
                continue
            parsed = getattr(res, "parsed", None)
            if isinstance(parsed, dict):
                return parsed
        return None

    def _complete_structured(self, user_text: str) -> dict | None:
        return self._structured(_SYS, user_text)

    def finalize(self, members: list[str]) -> dict | None:
        if not members or not self.ok:
            return None
        block = "\n".join(f"[{i}] {t}" for i, t in enumerate(members))
        obj = self._complete_structured(block)
        if not isinstance(obj, dict):
            return None
        topic = str(obj.get("topic") or "").strip()[:40]
        same = bool(obj.get("same_matter")) and bool(topic)
        return {"same_matter": same, "topic": topic, "corrections": obj.get("corrections") or []}

    def extract_from_summary(self, new_summary: str, prev_summary: str | None) -> dict | None:
        """Extract memories from a Hermes compaction summary.

        First compaction (prev_summary is None): extract all durable memories.
        Later compactions: extract ONLY what changed vs the previous summary.
        (Hermes summaries are cumulative & budget-bounded, so a plain re-extract would
        duplicate; the delta compare keeps it cheap and non-redundant.)
        """
        if not self.ok or not (new_summary or "").strip():
            return None
        if prev_summary is None:
            obj = self._structured(self._sys(_SEG_SYS), new_summary)
        else:
            text = f"【上一版摘要】\n{prev_summary}\n\n【最新摘要】\n{new_summary}"
            obj = self._structured(self._sys(_DELTA_SYS), text)
        if not isinstance(obj, dict):
            return None
        mems = obj.get("memories") or []
        return {"memories": mems if isinstance(mems, list) else []}

    def extract_from_messages(self, messages: list[dict]) -> dict | None:
        """Chat batch: extract durable memories from a window of raw conversation.

        Returns ``{"memories": [...]}`` on SUCCESS (possibly empty), or ``None`` on
        FAILURE (LLM unavailable / unparseable) so the caller keeps the cursor & retries.
        """
        if not self.ok or not messages:
            return None
        block = "\n".join(f"{m.get('role', 'user')}: {m.get('content', '')}" for m in messages)
        obj = self._structured(self._sys(_MSG_SYS), block)
        if not isinstance(obj, dict):
            return None
        mems = obj.get("memories") or []
        return {"memories": mems if isinstance(mems, list) else []}

    def review_verdict(self, candidate: str, prior: str) -> dict | None:
        """Decide whether an inactive candidate is the SAME matter as a prior memory and,
        if so, whether it UPDATES (bumps a new version) or merely CORROBORATES it.
        Returns {"same": bool, "update": bool} or None on failure."""
        if not self.ok:
            return None
        text = f"【旧记忆】\n{prior}\n\n【新候选】\n{candidate}"
        obj = self._structured(_REVIEW_SYS, text)
        if not isinstance(obj, dict) or "same" not in obj:
            return None
        return {"same": bool(obj.get("same")), "update": bool(obj.get("update"))}
