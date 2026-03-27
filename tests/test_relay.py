from __future__ import annotations

import os

from exectunnel.payload import agent


def test_relay_connection_feed_saturation_emits_error_and_closes(monkeypatch) -> None:
    emitted: list[str] = []
    monkeypatch.setattr(agent, "_emit_ctrl", lambda line: emitted.append(line))

    conn = agent.Connection("cabc123", "example.com", 443)
    try:
        conn._inbound = [b"x"] * agent._MAX_TCP_INBOUND_CHUNKS  # type: ignore[attr-defined]
        conn.feed(b"overflow")
        conn.feed(b"ignored-after-saturation")
        assert conn._saturated is True  # type: ignore[attr-defined]
        assert conn._closed is True  # type: ignore[attr-defined]
        assert len(emitted) == 1
        assert emitted[0].startswith("<<<EXECTUNNEL:ERROR:cabc123:")
    finally:
        os.close(conn._notify_r)  # type: ignore[attr-defined]
        os.close(conn._notify_w)  # type: ignore[attr-defined]
