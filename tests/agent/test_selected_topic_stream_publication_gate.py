from typing import Any

from agent.stream_delivery import StreamDeliveryMixin


class _Agent(StreamDeliveryMixin):
    reasoning_callback: Any = None
    stream_delta_callback: Any = None

    def __init__(self):
        self._defer_final_response_stream_delivery = True
        self._stream_reasoning_hooks_enabled = False
        self._streamed_assistant_text_parts = []
        self._native_reasoning_streamed = False
        self.hooks = []
        self.reasoning_callback = None
        self.stream_delta_callback = None
        self._stream_callback = None

    def _stream_writer_superseded(self):
        return False

    def _note_dropped_stream_writer(self, where):
        raise AssertionError("unexpected stale writer")

    def _strip_think_blocks(self, text):
        return text

    def _enqueue_stream_hook(self, event, **kwargs):
        self.hooks.append((event, kwargs))


def test_selected_topic_buffers_reasoning_then_releases_before_text():
    events = []
    agent = _Agent()
    agent.reasoning_callback = lambda text: events.append(("reasoning", text))
    agent.stream_delta_callback = lambda text: events.append(("text", text))

    agent._fire_reasoning_delta("private reasoning")
    agent._fire_stream_delta("candidate response")

    assert events == []
    assert agent.hooks == []
    agent._release_deferred_final_response("accepted response")
    assert events == [
        ("reasoning", "private reasoning"),
        ("text", "accepted response"),
    ]


def test_selected_topic_discards_reasoning_and_text_on_failure():
    events = []
    agent = _Agent()
    agent.reasoning_callback = lambda text: events.append(("reasoning", text))
    agent.stream_delta_callback = lambda text: events.append(("text", text))

    agent._fire_reasoning_delta("private reasoning")
    agent._fire_stream_delta("candidate response")
    agent._discard_deferred_final_response()

    assert events == []
    assert agent._deferred_reasoning_deltas == []
