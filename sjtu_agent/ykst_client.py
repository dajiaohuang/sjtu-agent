"""Native YKST / TreeHole gRPC-Web client.

This module intentionally implements only the small protobuf surface that
sjtu-agent needs: login, profile/identities, thread/post reads, replies,
ratings, and favorites. It mirrors the ykst-treehole-mcp request flow without
requiring Node.js or an external MCP server for native support.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Any

import requests

from sjtu_agent import paths


DEFAULT_HOST = "https://proxy.treehole.qaq.ac.cn"
DEFAULT_REDIRECT_URI = "https://web.treehole.space/auth/jaccount"

LOGIN_WITH_JACCOUNT = 0
LOGIN_SOURCE_WEB = 2
WEB_SOURCE_PROD_SERVER = 2
SORT_ASC = 0
SORT_DESC = 1
LOAD_DIRECTION_DOWN = 0

RATE_NORMAL = 0
RATE_HATE = -1
RATE_LIKE = 1


def _find_chrome() -> str | None:
    env_chrome = os.environ.get("CHROME_PATH")
    if env_chrome and Path(env_chrome).exists():
        return env_chrome

    if sys.platform == "win32":
        candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES", ""), r"Google\Chrome\Application\chrome.exe"),
            os.path.join(os.environ.get("PROGRAMFILES(X86)", ""), r"Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        candidates = [
            "google-chrome",
            "google-chrome-stable",
            "chromium",
            "chromium-browser",
        ]

    for c in candidates:
        if c and Path(c).exists():
            return c

    for name in ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]:
        found = shutil.which(name)
        if found:
            return found

    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class YKSTError(RuntimeError):
    """Base exception for YKST client errors."""


class YKSTAuthError(YKSTError):
    """Raised when an authenticated call has no token."""


class YKSTProtocolError(YKSTError):
    """Raised for malformed protobuf or gRPC-Web responses."""


PBValue = tuple[int, int | bytes]
PBMessage = dict[int, list[PBValue]]


def _read_config() -> dict:
    if not paths.CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_config(updates: dict) -> None:
    cfg = _read_config()
    cfg.update(updates)
    paths.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    paths.CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _token_from_env() -> str:
    return (os.environ.get("TREEHOLE_SESSION") or os.environ.get("TREEHOLE_TOKEN") or "").strip()


def get_host() -> str:
    host = (
        os.environ.get("TREEHOLE_RPC_HOST")
        or _read_config().get("ykst_treehole_host")
        or DEFAULT_HOST
    )
    return str(host).strip().rstrip("/") or DEFAULT_HOST


def get_token() -> str:
    return _token_from_env() or str(_read_config().get("ykst_treehole_token") or "").strip()


def auth_status() -> dict:
    token = get_token()
    source = None
    if _token_from_env():
        source = "env:TREEHOLE_SESSION/TREEHOLE_TOKEN"
    elif token:
        source = "config:ykst_treehole_token"
    return {
        "authenticated": bool(token),
        "host": get_host(),
        "token_source": source,
        "token_hint": f"...{token[-6:]}" if token else None,
        "setup_tool": "setup_ykst",
    }


def save_session_token(token: str, host: str | None = None) -> dict:
    token = (token or "").strip()
    if not token:
        raise YKSTError("empty YKST session token")
    saved_host = (host or get_host() or DEFAULT_HOST).strip().rstrip("/")
    _write_config({"ykst_treehole_token": token, "ykst_treehole_host": saved_host})
    return {
        "success": True,
        "authenticated": True,
        "host": saved_host,
        "token_hint": f"...{token[-6:]}",
    }


def _varint(value: int) -> bytes:
    if value < 0:
        value = (1 << 64) + value
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    shift = 0
    value = 0
    for idx in range(offset, len(data)):
        byte = data[idx]
        value |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return value, idx + 1
        shift += 7
        if shift >= 70:
            raise YKSTProtocolError("protobuf varint is too long")
    raise YKSTProtocolError("truncated protobuf varint")


def _key(field: int, wire_type: int) -> bytes:
    return _varint((field << 3) | wire_type)


def _pb_varint(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(int(value))


def _pb_bool(field: int, value: bool) -> bytes:
    return _pb_varint(field, 1 if value else 0)


def _pb_string(field: int, value: str) -> bytes:
    raw = str(value).encode("utf-8")
    return _key(field, 2) + _varint(len(raw)) + raw


def _pb_message(field: int, value: bytes) -> bytes:
    return _key(field, 2) + _varint(len(value)) + value


def _bool_wrapper(value: bool) -> bytes:
    return _pb_bool(1, bool(value))


def _uint64_wrapper(value: int) -> bytes:
    return _pb_varint(1, int(value))


def _string_wrapper(value: str) -> bytes:
    return _pb_string(1, value)


def decode_message(data: bytes) -> PBMessage:
    msg: PBMessage = {}
    offset = 0
    while offset < len(data):
        tag, offset = _read_varint(data, offset)
        field = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:
            value, offset = _read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise YKSTProtocolError("truncated fixed64 field")
            value = int.from_bytes(data[offset:offset + 8], "little")
            offset += 8
        elif wire_type == 2:
            length, offset = _read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise YKSTProtocolError("truncated length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise YKSTProtocolError("truncated fixed32 field")
            value = int.from_bytes(data[offset:offset + 4], "little")
            offset += 4
        else:
            raise YKSTProtocolError(f"unsupported protobuf wire type: {wire_type}")
        msg.setdefault(field, []).append((wire_type, value))
    return msg


def _values(msg: PBMessage, field: int, wire_type: int | None = None) -> list[int | bytes]:
    values = msg.get(field, [])
    if wire_type is not None:
        values = [item for item in values if item[0] == wire_type]
    return [item[1] for item in values]


def _last_varint(msg: PBMessage, field: int, default: int = 0) -> int:
    values = _values(msg, field, 0)
    return int(values[-1]) if values else default


def _last_bool(msg: PBMessage, field: int, default: bool = False) -> bool:
    values = _values(msg, field, 0)
    return bool(values[-1]) if values else default


def _last_string(msg: PBMessage, field: int, default: str = "") -> str:
    values = _values(msg, field, 2)
    if not values:
        return default
    return bytes(values[-1]).decode("utf-8", errors="replace")


def _messages(msg: PBMessage, field: int) -> list[bytes]:
    return [bytes(value) for value in _values(msg, field, 2)]


def _first_message(msg: PBMessage, field: int) -> bytes | None:
    items = _messages(msg, field)
    return items[-1] if items else None


def _parse_timestamp(data: bytes | None) -> dict | None:
    if not data:
        return None
    msg = decode_message(data)
    return {
        "seconds": _last_varint(msg, 1, 0),
        "nanos": _last_varint(msg, 2, 0),
    }


def _parse_model(data: bytes | None) -> dict | None:
    if not data:
        return None
    msg = decode_message(data)
    model = {"id": _last_varint(msg, 1, 0)}
    created_at = _parse_timestamp(_first_message(msg, 2))
    updated_at = _parse_timestamp(_first_message(msg, 3))
    deleted_at = _parse_timestamp(_first_message(msg, 4))
    if created_at:
        model["createdAt"] = created_at
    if updated_at:
        model["updatedAt"] = updated_at
    if deleted_at:
        model["deletedAt"] = deleted_at
    return model


def _parse_bool_wrapper(data: bytes | None) -> bool | None:
    if data is None:
        return None
    return _last_bool(decode_message(data), 1, False)


def _parse_uint64_wrapper(data: bytes | None) -> int | None:
    if data is None:
        return None
    return _last_varint(decode_message(data), 1, 0)


def _parse_string_wrapper(data: bytes | None) -> str | None:
    if data is None:
        return None
    return _last_string(decode_message(data), 1, "")


def _parse_identity(data: bytes | None) -> dict | None:
    if not data:
        return None
    msg = decode_message(data)
    return {
        "model": _parse_model(_first_message(msg, 1)),
        "userId": _last_varint(msg, 2, 0),
        "code": _last_string(msg, 3, ""),
        "status": _last_varint(msg, 4, 0),
        "type": _last_varint(msg, 5, 0),
        "isActive": _last_bool(msg, 6, False),
        "remark": _last_string(msg, 7, ""),
        "isSpecial": _last_bool(msg, 8, False),
    }


def _parse_user(data: bytes) -> dict:
    msg = decode_message(data)
    identities = [_parse_identity(raw) for raw in _messages(msg, 3)]
    return {
        "model": _parse_model(_first_message(msg, 1)),
        "account": _last_string(msg, 2, ""),
        "identitiesList": [item for item in identities if item is not None],
        "status": _last_varint(msg, 4, 0),
        "role": _last_varint(msg, 5, 0),
    }


def _parse_thread(data: bytes | None, depth: int = 0) -> dict | None:
    if not data:
        return None
    msg = decode_message(data)
    out = {
        "model": _parse_model(_first_message(msg, 1)),
        "title": _last_string(msg, 2, ""),
        "categoryId": _last_varint(msg, 3, 0),
        "identityCode": _last_string(msg, 6, ""),
        "content": _last_string(msg, 7, ""),
        "viewCount": _last_varint(msg, 8, 0),
        "likeCount": _last_varint(msg, 9, 0),
        "hateCount": _last_varint(msg, 10, 0),
        "replyCount": _last_varint(msg, 11, 0),
        "isTop": _last_bool(msg, 12, False),
        "isFav": _last_bool(msg, 13, False),
        "isLike": _last_bool(msg, 14, False),
        "isHate": _last_bool(msg, 15, False),
        "status": _last_varint(msg, 16, 0),
        "lastReplyAt": _last_varint(msg, 17, 0),
        "identity": _parse_identity(_first_message(msg, 18)),
        "isSage": _last_bool(msg, 19, False),
        "isReadOnly": _last_bool(msg, 20, False),
        "hasRead": _last_bool(msg, 24, False),
        "appreciationCount": _last_varint(msg, 25, 0),
        "isAppreciated": _last_bool(msg, 26, False),
        "canDelete": _last_bool(msg, 27, False),
        "remark": _last_string(msg, 29, ""),
        "preview": _last_string(msg, 30, ""),
        "hideReason": _last_string(msg, 32, ""),
    }
    is_alice = _parse_bool_wrapper(_first_message(msg, 22))
    last_read = _parse_uint64_wrapper(_first_message(msg, 23))
    disable_hate = _parse_bool_wrapper(_first_message(msg, 31))
    if is_alice is not None:
        out["isAlice"] = is_alice
    if last_read is not None:
        out["lastRead"] = last_read
    if disable_hate is not None:
        out["disableHate"] = disable_hate
    return out


def _parse_post(data: bytes | None, depth: int = 0) -> dict | None:
    if not data:
        return None
    msg = decode_message(data)
    out = {
        "model": _parse_model(_first_message(msg, 1)),
        "threadId": _last_varint(msg, 2, 0),
        "floor": _last_varint(msg, 3, 0),
        "identityCode": _last_string(msg, 4, ""),
        "content": _last_string(msg, 5, ""),
        "likeCount": _last_varint(msg, 6, 0),
        "hateCount": _last_varint(msg, 7, 0),
        "status": _last_varint(msg, 8, 0),
        "replyToPostId": _parse_uint64_wrapper(_first_message(msg, 9)),
        "replyToIdentityCode": _parse_string_wrapper(_first_message(msg, 10)),
        "replyToFloor": _parse_uint64_wrapper(_first_message(msg, 11)),
        "isLike": _last_bool(msg, 12, False),
        "isHate": _last_bool(msg, 13, False),
        "identity": _parse_identity(_first_message(msg, 14)),
        "hideIdentity": _parse_bool_wrapper(_first_message(msg, 17)),
        "userThreadIdentityId": _parse_uint64_wrapper(_first_message(msg, 18)),
        "appreciationCount": _last_varint(msg, 20, 0),
        "isAppreciated": _last_bool(msg, 21, False),
        "canDelete": _last_bool(msg, 22, False),
        "remark": _last_string(msg, 23, ""),
        "hideReason": _last_string(msg, 24, ""),
        "preview": _last_string(msg, 25, ""),
        "disableHate": _parse_bool_wrapper(_first_message(msg, 26)),
    }
    if depth < 1:
        reply_to_post = _parse_post(_first_message(msg, 15), depth + 1)
        thread = _parse_thread(_first_message(msg, 16), depth + 1)
        if reply_to_post:
            out["replyToPost"] = reply_to_post
        if thread:
            out["thread"] = thread
    return out


def _parse_oauth_config_response(data: bytes) -> dict:
    msg = decode_message(data)
    scopes = [bytes(value).decode("utf-8", errors="replace") for value in _values(msg, 3, 2)]
    return {
        "authorizeUrl": _last_string(msg, 1, ""),
        "clientId": _last_string(msg, 2, ""),
        "scopesList": scopes,
    }


def _parse_oauth_login_response(data: bytes) -> dict:
    msg = decode_message(data)
    return {"token": _last_string(msg, 1, "")}


def _parse_threads_response(data: bytes) -> dict:
    msg = decode_message(data)
    threads = [_parse_thread(raw) for raw in _messages(msg, 1)]
    return {"threads": [item for item in threads if item is not None]}


def _parse_posts_response(data: bytes) -> dict:
    msg = decode_message(data)
    posts = [_parse_post(raw) for raw in _messages(msg, 1)]
    return {
        "posts": [item for item in posts if item is not None],
        "total": _last_varint(msg, 2, 0),
    }


def _encode_model(model_id: int | None) -> bytes:
    if not model_id:
        return b""
    return _pb_varint(1, int(model_id))


def _encode_identity(identity: dict) -> bytes:
    parts: list[bytes] = []
    model = identity.get("model") or {}
    model_id = model.get("id")
    if model_id:
        parts.append(_pb_message(1, _encode_model(model_id)))
    if identity.get("userId"):
        parts.append(_pb_varint(2, int(identity["userId"])))
    if identity.get("code"):
        parts.append(_pb_string(3, str(identity["code"])))
    if identity.get("status"):
        parts.append(_pb_varint(4, int(identity["status"])))
    if identity.get("type"):
        parts.append(_pb_varint(5, int(identity["type"])))
    if identity.get("isActive"):
        parts.append(_pb_bool(6, True))
    if identity.get("remark"):
        parts.append(_pb_string(7, str(identity["remark"])))
    if identity.get("isSpecial"):
        parts.append(_pb_bool(8, True))
    return b"".join(parts)


def _limit(value: int | None, default: int = 20, maximum: int = 50) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return min(max(number, 1), maximum)


def _encode_oauth_config_request() -> bytes:
    return _pb_varint(2, LOGIN_SOURCE_WEB)


def _encode_oauth_login_request(code: str) -> bytes:
    return b"".join([
        _pb_string(1, code),
        _pb_varint(3, LOGIN_SOURCE_WEB),
        _pb_varint(4, WEB_SOURCE_PROD_SERVER),
    ])


def _encode_id_request(item_id: int) -> bytes:
    return _pb_varint(1, int(item_id))


def _encode_posts_query_request(thread_id: int, should_statistic: bool = False) -> bytes:
    parts = [_pb_varint(1, int(thread_id))]
    if should_statistic:
        parts.append(_pb_bool(7, True))
    return b"".join(parts)


def _encode_posts_query_request_ex(
    thread_id: int,
    limit: int = 15,
    cursor: int = 0,
    top: int = 0,
    only_author: bool = False,
    sort: int = SORT_ASC,
) -> bytes:
    parts = [_pb_varint(1, int(thread_id))]
    if cursor:
        parts.append(_pb_varint(2, int(cursor)))
    if top:
        parts.append(_pb_varint(3, int(top)))
    parts.append(_pb_varint(4, _limit(limit, 15)))
    if sort:
        parts.append(_pb_varint(5, int(sort)))
    if only_author:
        parts.append(_pb_bool(6, True))
    if LOAD_DIRECTION_DOWN:
        parts.append(_pb_varint(7, LOAD_DIRECTION_DOWN))
    return b"".join(parts)


def _encode_search_request(keyword: str, limit: int = 20, offset: int = 0) -> bytes:
    parts = [_pb_string(1, keyword), _pb_varint(2, _limit(limit, 20))]
    if offset:
        parts.append(_pb_varint(3, int(offset)))
    return b"".join(parts)


def _encode_rate_request(item_id: int, rate_type: str | int) -> bytes:
    value = rate_type_value(rate_type)
    parts = [_pb_varint(1, int(item_id))]
    if value != 0:
        parts.append(_pb_varint(2, value))
    return b"".join(parts)


def _encode_fav_request(thread_id: int, is_fav: bool) -> bytes:
    parts = [_pb_varint(1, int(thread_id))]
    if is_fav:
        parts.append(_pb_bool(2, True))
    return b"".join(parts)


def _encode_post_request(
    thread_id: int,
    content: str,
    identity: dict,
    hide_identity: bool = False,
    reply_to_post_id: int | None = None,
    user_thread_identity_id: int | None = None,
) -> bytes:
    parts = [
        _pb_varint(2, int(thread_id)),
        _pb_string(5, content),
        _pb_message(17, _bool_wrapper(bool(hide_identity))),
    ]
    if identity.get("code"):
        parts.append(_pb_string(4, str(identity["code"])))
    parts.append(_pb_message(14, _encode_identity(identity)))
    if reply_to_post_id is not None:
        parts.append(_pb_message(9, _uint64_wrapper(int(reply_to_post_id))))
    if user_thread_identity_id is not None:
        parts.append(_pb_message(18, _uint64_wrapper(int(user_thread_identity_id))))
    return b"".join(parts)


def _grpc_frame(payload: bytes, flags: int = 0) -> bytes:
    return bytes([flags]) + len(payload).to_bytes(4, "big") + payload


def _parse_trailers(raw: bytes) -> dict[str, str]:
    trailers: dict[str, str] = {}
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        trailers[key.strip().lower()] = value.strip()
    return trailers


def _decode_grpc_web_response(raw: bytes) -> tuple[bytes, dict[str, str]]:
    offset = 0
    payloads: list[bytes] = []
    trailers: dict[str, str] = {}
    while offset < len(raw):
        if offset + 5 > len(raw):
            raise YKSTProtocolError("truncated gRPC-Web frame header")
        flags = raw[offset]
        length = int.from_bytes(raw[offset + 1:offset + 5], "big")
        offset += 5
        end = offset + length
        if end > len(raw):
            raise YKSTProtocolError("truncated gRPC-Web frame payload")
        payload = raw[offset:end]
        offset = end
        if flags & 0x80:
            trailers.update(_parse_trailers(payload))
        elif flags == 0:
            payloads.append(payload)
        else:
            raise YKSTProtocolError(f"unsupported gRPC-Web frame flags: {flags}")
    return b"".join(payloads), trailers


def _rpc(method: str, request_bytes: bytes, auth: bool = True, timeout: int = 30) -> bytes:
    token = get_token()
    if auth and not token:
        raise YKSTAuthError("YKST 未配置登录态，请先对 Agent 说「配置树洞」或使用 setup_ykst。")

    headers = {
        "content-type": "application/grpc-web+proto",
        "accept": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "grpc-web-javascript/0.1",
    }
    if auth:
        headers["authorization"] = token

    url = f"{get_host()}{method}"
    resp = requests.post(url, headers=headers, data=_grpc_frame(request_bytes), timeout=timeout)
    if not resp.ok:
        raise YKSTError(f"YKST RPC {method} failed with HTTP {resp.status_code}: {resp.text[:300]}")

    payload, trailers = _decode_grpc_web_response(resp.content)
    grpc_status = trailers.get("grpc-status")
    if grpc_status not in (None, "0"):
        message = urllib.parse.unquote(trailers.get("grpc-message", ""))
        raise YKSTError(f"YKST RPC {method} failed with grpc-status {grpc_status}: {message}")
    return payload


def get_login_url(redirect_uri: str = DEFAULT_REDIRECT_URI) -> dict:
    data = _rpc("/model.TreeHole/GetOAuthConfig", _encode_oauth_config_request(), auth=False)
    config = _parse_oauth_config_response(data)
    authorize_url = config.get("authorizeUrl")
    client_id = config.get("clientId")
    if not authorize_url or not client_id:
        raise YKSTError("YKST OAuth config response is missing authorizeUrl/clientId")

    parsed = urllib.parse.urlparse(authorize_url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    query.update({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(config.get("scopesList") or []),
    })
    login_url = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
    return {**config, "redirectUri": redirect_uri, "loginUrl": login_url}


def _wait_for_callback_url(port: int, timeout_ms: int = 180000) -> str:
    started = time.monotonic()
    while (time.monotonic() - started) * 1000 < timeout_ms:
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/json/list", timeout=2)
            targets = resp.json()
            for target in targets:
                url = target.get("url", "")
                if "/auth/jaccount" in url:
                    parsed = urllib.parse.urlparse(url)
                    code = dict(urllib.parse.parse_qsl(parsed.query)).get("code")
                    if code:
                        return url
        except Exception:
            pass
        time.sleep(0.8)
    raise YKSTError("等待浏览器登录超时（3 分钟），请重新尝试或使用手动流程。")


def login_with_browser_watch(
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    timeout_ms: int = 180000,
) -> dict:
    chrome = _find_chrome()
    if not chrome:
        raise YKSTError("未找到 Chrome 浏览器，请设置 CHROME_PATH 环境变量后重试，或使用手动流程。")

    info = get_login_url(redirect_uri)
    login_url = info["loginUrl"]

    port = int(os.environ.get("TREEHOLE_LOGIN_DEBUG_PORT", "0")) or _free_port()

    profile = os.environ.get(
        "TREEHOLE_LOGIN_CHROME_PROFILE",
        str(Path(tempfile.gettempdir()) / "sjtu-agent-ykst-login"),
    )
    Path(profile).mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            f"--remote-debugging-port={port}",
            "--new-window",
            login_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        callback_url = _wait_for_callback_url(port, timeout_ms)
        return login_with_callback_url(callback_url)
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


def login_with_code(code: str) -> dict:
    code = (code or "").strip()
    if not code:
        raise YKSTError("empty YKST OAuth code")
    data = _rpc("/model.TreeHole/OAuthLogin", _encode_oauth_login_request(code), auth=False)
    token = _parse_oauth_login_response(data).get("token", "")
    if not token:
        raise YKSTError("YKST OAuth login did not return a token")
    return save_session_token(token, get_host())


def login_with_callback_url(callback_url: str) -> dict:
    parsed = urllib.parse.urlparse((callback_url or "").strip())
    code = dict(urllib.parse.parse_qsl(parsed.query)).get("code", "")
    if not code:
        raise YKSTError("callback_url does not contain a code query parameter")
    return login_with_code(code)


def profile() -> dict:
    return _parse_user(_rpc("/model.TreeHole/GetProfile", b""))


def list_identities() -> dict:
    user = profile()
    return {
        "account": user.get("account"),
        "identities": user.get("identitiesList") or [],
    }


def get_identity(identity_id: int | None = None, code: str | None = None, active: bool = False) -> dict:
    identities = list_identities().get("identities", [])
    for identity in identities:
        model = identity.get("model") or {}
        if active and identity.get("isActive"):
            return identity
        if identity_id is not None and int(model.get("id") or 0) == int(identity_id):
            return identity
        if code and str(identity.get("code", "")).lower() == str(code).lower():
            return identity
    raise YKSTError("Identity not found")


def set_active_identity(identity_id: int) -> dict:
    return _parse_user(_rpc("/model.TreeHole/SetActiveIdentity", _encode_id_request(identity_id)))


def search_threads(keyword: str, limit: int = 20, offset: int = 0) -> dict:
    keyword = (keyword or "").strip()
    if not keyword:
        raise YKSTError("keyword is required")
    result = _parse_threads_response(
        _rpc("/model.TreeHole/SearchThreads", _encode_search_request(keyword, limit, offset))
    )
    result["nextOffset"] = int(offset or 0) + len(result.get("threads") or [])
    return result


def get_thread(thread_id: int) -> dict:
    data = _rpc("/model.TreeHole/GetThread", _encode_posts_query_request(thread_id, should_statistic=True))
    thread = _parse_thread(data)
    return thread or {}


def get_post(post_id: int) -> dict:
    data = _rpc("/model.TreeHole/GetPost", _pb_message(1, _encode_model(post_id)))
    post = _parse_post(data)
    return post or {}


def get_thread_posts(
    thread_id: int,
    limit: int = 15,
    cursor: int = 0,
    top: int = 0,
    only_author: bool = False,
) -> dict:
    result = _parse_posts_response(
        _rpc(
            "/model.TreeHole/GetThreadPostsEx",
            _encode_posts_query_request_ex(thread_id, limit, cursor, top, only_author),
        )
    )
    posts = result.get("posts") or []
    result["nextCursor"] = posts[-1].get("floor", 0) if posts else 0
    return result


def reply_thread(
    thread_id: int,
    content: str,
    hide_identity: bool = False,
    reply_to_post_id: int | None = None,
    user_thread_identity_id: int | None = None,
) -> dict:
    content = (content or "").strip()
    if not content:
        raise YKSTError("content is required")
    identity = get_identity(active=True)
    request = _encode_post_request(
        thread_id,
        content,
        identity,
        hide_identity=hide_identity,
        reply_to_post_id=reply_to_post_id,
        user_thread_identity_id=user_thread_identity_id,
    )
    return _parse_post(_rpc("/model.TreeHole/PutPost", request)) or {}


def rate_type_value(value: str | int) -> int:
    normalized = str(value if value is not None else "normal").lower()
    if normalized in ("like", "1"):
        return RATE_LIKE
    if normalized in ("hate", "-1", "dislike"):
        return RATE_HATE
    return RATE_NORMAL


def rate_thread(thread_id: int, type: str | int = "like") -> dict:
    data = _rpc("/model.TreeHole/RateThread", _encode_rate_request(thread_id, type))
    return _parse_thread(data) or {}


def rate_post(post_id: int, type: str | int = "like") -> dict:
    data = _rpc("/model.TreeHole/RatePost", _encode_rate_request(post_id, type))
    return _parse_post(data) or {}


def favorite_thread(thread_id: int, is_fav: bool = True) -> dict:
    data = _rpc("/model.TreeHole/FavThread", _encode_fav_request(thread_id, is_fav))
    return _parse_thread(data) or {}


def session_file_hint() -> str:
    return str(Path.home() / ".treehole-session.json")
