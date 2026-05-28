import json


def test_ykst_write_tools_require_confirmation():
    from sjtu_agent.agent import tools

    reply = tools.tool_ykst_reply_thread(thread_id=123, content="hello")
    switch_identity = tools.tool_ykst_set_active_identity(identity_id=42)
    rate = tools.tool_ykst_rate_thread(thread_id=123)
    favorite = tools.tool_ykst_favorite_thread(thread_id=123)

    assert reply["requires_confirmation"] is True
    assert switch_identity["requires_confirmation"] is True
    assert rate["requires_confirmation"] is True
    assert favorite["requires_confirmation"] is True


def test_ykst_run_tool_dispatches_auth_status(monkeypatch):
    from sjtu_agent import ykst_client
    from sjtu_agent.agent import tools

    monkeypatch.setattr(
        ykst_client,
        "auth_status",
        lambda: {"authenticated": False, "host": "https://proxy.example.test"},
    )

    result = json.loads(tools.run_tool("ykst_auth_status", {}))

    assert result == {"authenticated": False, "host": "https://proxy.example.test"}


def test_ykst_confirmed_tool_calls_client(monkeypatch):
    from sjtu_agent import ykst_client
    from sjtu_agent.agent import tools

    calls = []

    def fake_rate_thread(thread_id, type="like"):
        calls.append((thread_id, type))
        return {"ok": True, "thread_id": thread_id, "type": type}

    monkeypatch.setattr(ykst_client, "rate_thread", fake_rate_thread)

    result = tools.tool_ykst_rate_thread(thread_id=123, type="normal", confirm=True)

    assert result == {"ok": True, "thread_id": 123, "type": "normal"}
    assert calls == [(123, "normal")]
