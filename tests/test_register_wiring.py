"""register(ctx) wiring: provider registered + auxiliary task declared."""

import hitmind.adapters.hermes as H


class _FakeCtx:
    def __init__(self):
        self.provider = None
        self.aux = []

    def register_memory_provider(self, provider):
        self.provider = provider

    def register_auxiliary_task(self, key, **kw):
        self.aux.append((key, kw))


def test_register_wires_provider_and_aux_task():
    ctx = _FakeCtx()
    H.register(ctx)
    assert isinstance(ctx.provider, H.HitMindMemoryProvider)
    assert ctx.aux and ctx.aux[0][0] == "hitmind_topic"


def test_register_survives_missing_aux_support():
    class _Bare:
        def __init__(self):
            self.provider = None

        def register_memory_provider(self, provider):
            self.provider = provider

    b = _Bare()  # no register_auxiliary_task -> must not break provider registration
    H.register(b)
    assert isinstance(b.provider, H.HitMindMemoryProvider)
