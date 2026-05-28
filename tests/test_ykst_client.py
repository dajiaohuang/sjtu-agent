import json


def test_get_login_url_builds_from_oauth_config(monkeypatch):
    from sjtu_agent import ykst_client as ykst

    def fake_rpc(method, request, auth=True, timeout=30):
        assert method == "/model.TreeHole/GetOAuthConfig"
        assert auth is False
        assert request == ykst._encode_oauth_config_request()
        return b"".join([
            ykst._pb_string(1, "https://jaccount.sjtu.edu.cn/oauth2/authorize"),
            ykst._pb_string(2, "treehole-client"),
            ykst._pb_string(3, "openid"),
            ykst._pb_string(3, "profile"),
        ])

    monkeypatch.setattr(ykst, "_rpc", fake_rpc)

    info = ykst.get_login_url("https://example.test/callback")

    assert info["authorizeUrl"] == "https://jaccount.sjtu.edu.cn/oauth2/authorize"
    assert info["clientId"] == "treehole-client"
    assert info["scopesList"] == ["openid", "profile"]
    assert "client_id=treehole-client" in info["loginUrl"]
    assert "redirect_uri=https%3A%2F%2Fexample.test%2Fcallback" in info["loginUrl"]
    assert "scope=openid+profile" in info["loginUrl"]


def test_login_with_callback_saves_token(tmp_path, monkeypatch):
    from sjtu_agent import paths
    from sjtu_agent import ykst_client as ykst

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(paths, "CONFIG_PATH", config_path)
    monkeypatch.setenv("TREEHOLE_RPC_HOST", "https://proxy.example.test")
    monkeypatch.delenv("TREEHOLE_SESSION", raising=False)
    monkeypatch.delenv("TREEHOLE_TOKEN", raising=False)

    def fake_rpc(method, request, auth=True, timeout=30):
        assert method == "/model.TreeHole/OAuthLogin"
        assert auth is False
        decoded = ykst.decode_message(request)
        assert ykst._last_string(decoded, 1) == "abc123"
        return ykst._pb_string(1, "session-token")

    monkeypatch.setattr(ykst, "_rpc", fake_rpc)

    result = ykst.login_with_callback_url("https://web.treehole.space/auth/jaccount?code=abc123")

    assert result["authenticated"] is True
    assert result["host"] == "https://proxy.example.test"
    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["ykst_treehole_token"] == "session-token"
    assert saved["ykst_treehole_host"] == "https://proxy.example.test"


def test_list_identities_extracts_active(monkeypatch):
    from sjtu_agent import ykst_client as ykst

    first = b"".join([
        ykst._pb_message(1, ykst._encode_model(11)),
        ykst._pb_varint(2, 100),
        ykst._pb_string(3, "ALPHA"),
    ])
    second = b"".join([
        ykst._pb_message(1, ykst._encode_model(12)),
        ykst._pb_varint(2, 100),
        ykst._pb_string(3, "BETA"),
        ykst._pb_bool(6, True),
        ykst._pb_string(7, "active one"),
    ])
    user = b"".join([
        ykst._pb_string(2, "student"),
        ykst._pb_message(3, first),
        ykst._pb_message(3, second),
    ])

    monkeypatch.setattr(ykst, "_rpc", lambda *args, **kwargs: user)

    result = ykst.list_identities()
    active = ykst.get_identity(active=True)

    assert result["account"] == "student"
    assert [item["code"] for item in result["identities"]] == ["ALPHA", "BETA"]
    assert active["model"]["id"] == 12
    assert active["remark"] == "active one"


def test_reply_thread_uses_active_identity_and_encodes_post(monkeypatch):
    from sjtu_agent import ykst_client as ykst

    active_identity = b"".join([
        ykst._pb_message(1, ykst._encode_model(42)),
        ykst._pb_varint(2, 9),
        ykst._pb_string(3, "TREE"),
        ykst._pb_bool(6, True),
    ])
    user = b"".join([
        ykst._pb_string(2, "student"),
        ykst._pb_message(3, active_identity),
    ])
    returned_post = b"".join([
        ykst._pb_message(1, ykst._encode_model(777)),
        ykst._pb_varint(2, 123),
        ykst._pb_string(4, "TREE"),
        ykst._pb_string(5, "hello"),
    ])
    captured = {}

    def fake_rpc(method, request, auth=True, timeout=30):
        if method == "/model.TreeHole/GetProfile":
            return user
        if method == "/model.TreeHole/PutPost":
            captured["request"] = request
            return returned_post
        raise AssertionError(method)

    monkeypatch.setattr(ykst, "_rpc", fake_rpc)

    result = ykst.reply_thread(123, "hello", hide_identity=True, reply_to_post_id=456)
    encoded = ykst.decode_message(captured["request"])

    assert result["model"]["id"] == 777
    assert ykst._last_varint(encoded, 2) == 123
    assert ykst._last_string(encoded, 4) == "TREE"
    assert ykst._last_string(encoded, 5) == "hello"
    assert ykst._parse_bool_wrapper(ykst._first_message(encoded, 17)) is True
    assert ykst._parse_uint64_wrapper(ykst._first_message(encoded, 9)) == 456
    assert ykst._parse_identity(ykst._first_message(encoded, 14))["code"] == "TREE"
