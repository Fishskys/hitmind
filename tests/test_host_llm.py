"""HostClusterLLM: task-preferred + main-model fallback, structured parse."""

from hitmind.adapters.hermes.llm import HostClusterLLM


class _Res:
    def __init__(self, parsed=None, text=""):
        self.parsed = parsed
        self.text = text


class _FakePluginLlm:
    """Mimics PluginLlm.complete_structured; task=hitmind_topic is 'unregistered'."""

    def __init__(self, verdict, deny_task=True):
        self._verdict = verdict
        self._deny = deny_task
        self.calls = []

    def complete_structured(self, *, instructions, input, json_mode, task=None):
        self.calls.append(task)
        if task is not None and self._deny:
            raise RuntimeError("task routing denied")
        return _Res(parsed=self._verdict)


def test_uses_main_model_when_task_not_registered():
    fake = _FakePluginLlm({"same_matter": True, "topic": "欠账进度", "corrections": []}, deny_task=True)
    h = HostClusterLLM(fake)
    v = h.finalize(["[0] 欠三件", "[1] 还一件"])
    assert v == {"same_matter": True, "topic": "欠账进度", "corrections": []}
    assert fake.calls == ["hitmind_topic", None]  # tried task, fell back to main


def test_uses_task_when_registered():
    fake = _FakePluginLlm({"same_matter": False, "topic": "", "corrections": []}, deny_task=False)
    h = HostClusterLLM(fake)
    v = h.finalize(["[0] 咖啡", "[1] 花盆"])
    assert v["same_matter"] is False
    assert fake.calls == ["hitmind_topic"]  # used the task, no fallback needed


def test_empty_or_no_llm_returns_none():
    assert HostClusterLLM(None).finalize(["[0] x"]) is None
    fake = _FakePluginLlm(None)
    assert HostClusterLLM(fake).finalize([]) is None
