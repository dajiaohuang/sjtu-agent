"""tests/test_icourse_ddl.py — icourse163 DDL 拉取的回归测试

锁定关键点：
  - fetch_icourse 不再依赖 ICOURSE_COURSES 等任何硬编码课程列表；
  - 必须先 warm-up 再调 RPC（否则 STUDY_* cookies 不被服务端识别）；
  - 通过 getMyLearnedCoursePanelList 动态发现所有课程及其当前 termId；
  - 已得分(score>0) 或 已过期(due<now) 的 quiz 不会出现在结果里。
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock

import pytest

import ddl_checker as dc

CST = timezone(timedelta(hours=8))


def _term_dto_with_quizs(quizs: list[dict]) -> dict:
    """构造一个最小的 getLastLearnedMocTermDto.rpc 响应（result 字段）。"""
    return {
        "lastLearnUnitId": 0,
        "mocTermDto": {
            "courseName": "测试课程",
            "id": 100,
            "chapters": [{"name": "第1章", "quizs": quizs}],
        },
    }


@pytest.fixture
def fake_session(monkeypatch):
    """伪造一个 requests.Session，记录所有调用以便断言顺序。"""
    s = MagicMock()
    s.cookies = MagicMock()
    s.cookies.get = MagicMock(return_value="csrf_xxx")
    # warm-up GET / 默认返回成功且把 STUDY_INFO 加到 .icourse163.org
    cookie_jar = []
    s.cookies.__iter__ = lambda self: iter(cookie_jar)

    def fake_get(url, **kwargs):
        if url.rstrip("/") == "https://www.icourse163.org":
            study = types.SimpleNamespace(name="STUDY_INFO", domain=".icourse163.org")
            cookie_jar.append(study)
            resp = MagicMock(status_code=200)
            return resp
        return MagicMock(status_code=200)

    s.get = MagicMock(side_effect=fake_get)
    monkeypatch.setattr(dc, "make_session", lambda cookies, referer="": s)
    return s


def test_warm_up_succeeds_with_valid_cookies(fake_session):
    assert dc._icourse_warm_up(fake_session) is True
    fake_session.get.assert_called_once_with("https://www.icourse163.org/", timeout=20)


def test_warm_up_fails_when_no_study_info(monkeypatch):
    s = MagicMock()
    s.cookies = MagicMock()
    s.cookies.__iter__ = lambda self: iter([])
    s.get = MagicMock(return_value=MagicMock(status_code=200))
    assert dc._icourse_warm_up(s) is False


def test_get_my_courses_extracts_termpanel_id():
    s = MagicMock()
    s.cookies.get = MagicMock(return_value="csrf")
    payload = {
        "code": 0,
        "result": {
            "result": [
                {
                    "id": 1449794172, "name": "大学物理—力学",
                    "termPanel": {"id": 1476751568},
                    "schoolPanel": {"shortName": "SJTU"},
                },
                {
                    "id": 1449785173, "name": "大学物理—热学",
                    "termPanel": {"id": 1487374444},
                    "schoolPanel": {"shortName": "SJTU"},
                },
            ],
            "pagination": {"totlePageCount": 1},
        },
    }
    empty = {"code": 0, "result": {"result": [], "pagination": {"totlePageCount": 1}}}
    s.post = MagicMock(side_effect=[
        MagicMock(status_code=200, json=lambda: payload),  # courseType=1
        MagicMock(status_code=200, json=lambda: empty),    # courseType=2 (SPOC)
    ])

    courses = dc._icourse_get_my_courses(s)
    assert len(courses) == 2
    assert {c["name"] for c in courses} == {"大学物理—力学", "大学物理—热学"}
    assert {c["term_id"] for c in courses} == {1476751568, 1487374444}
    assert all(c["school_short_name"] == "SJTU" for c in courses)


def test_get_my_courses_returns_none_when_unauth():
    s = MagicMock()
    s.cookies.get = MagicMock(return_value="bad")
    s.post = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"code": -1}))
    assert dc._icourse_get_my_courses(s) is None


def test_rpc_filters_done_and_expired(monkeypatch):
    now = datetime.now(CST)
    quizs = [
        # 已过期 → 排除
        {"id": 1, "name": "过期测试",
         "test": {"deadline": int((now - timedelta(days=1)).timestamp() * 1000)}},
        # 已得分 → 排除
        {"id": 2, "name": "已完成测试",
         "test": {"userScore": 80.0,
                  "deadline": int((now + timedelta(days=3)).timestamp() * 1000)}},
        # 未做且未过期 → 保留
        {"id": 3, "name": "待办测试",
         "test": {"deadline": int((now + timedelta(days=5)).timestamp() * 1000)}},
    ]
    result = dc._parse_icourse_rpc(_term_dto_with_quizs(quizs), cname="物理")
    assert len(result) == 1
    assert result[0]["name"] == "待办测试"
    assert result[0]["platform"] == "icourse163"
    assert result[0]["submitted"] is False


def test_fetch_icourse_end_to_end_dynamic_discovery(monkeypatch, fake_session):
    """主流程：warm-up → 拉课程列表 → 对每门课调 RPC → 聚合结果。"""
    monkeypatch.setattr(dc, "_icourse_get_my_courses", lambda s: [
        {"name": "力学", "course_id": 1, "term_id": 11, "school_short_name": "SJTU"},
        {"name": "热学", "course_id": 2, "term_id": 22, "school_short_name": "SJTU"},
    ])
    rpc_calls: list[tuple] = []

    now = datetime.now(CST)
    pending_quiz = [{"id": 9, "name": "第8周测试",
                     "test": {"deadline": int((now + timedelta(days=4)).timestamp() * 1000)}}]

    def fake_rpc(session, term_id, course_id=0, school=""):
        rpc_calls.append((term_id, course_id, school))
        return _term_dto_with_quizs(pending_quiz)

    monkeypatch.setattr(dc, "_icourse_rpc", fake_rpc)

    items = dc.fetch_icourse({"icourse_cookies": {"NTESSTUDYSI": "x"}})
    assert len(items) == 2
    assert {i["course"] for i in items} == {"力学", "热学"}
    # 必须对每门课都用正确的 termId、course_id、school 调 RPC
    assert sorted(rpc_calls) == [(11, 1, "SJTU"), (22, 2, "SJTU")]


def test_fetch_icourse_relogins_when_warmup_fails(monkeypatch):
    """cookies 失效时应该走账密登录回退。"""
    session_after_login = MagicMock()
    session_after_login.cookies = MagicMock()
    cookie_jar = [types.SimpleNamespace(name="STUDY_INFO", domain=".icourse163.org")]
    session_after_login.cookies.__iter__ = lambda self: iter(cookie_jar)
    session_after_login.cookies.get = MagicMock(return_value="new_csrf")
    session_after_login.get = MagicMock(return_value=MagicMock(status_code=200))

    bad_session = MagicMock()
    bad_session.cookies = MagicMock()
    bad_session.cookies.__iter__ = lambda self: iter([])  # warm-up 失败信号
    bad_session.cookies.get = MagicMock(return_value="bad")
    bad_session.get = MagicMock(return_value=MagicMock(status_code=200))

    sessions = iter([bad_session, session_after_login])
    monkeypatch.setattr(dc, "make_session", lambda cookies, referer="": next(sessions))
    monkeypatch.setattr(dc, "_icourse_login_with_creds",
                        lambda cfg: {"NTESSTUDYSI": "new"})
    monkeypatch.setattr(dc, "_icourse_get_my_courses", lambda s: [])

    items = dc.fetch_icourse({"icourse_cookies": {"NTESSTUDYSI": "old_invalid"}})
    assert items == []
    # 确认确实走了重登
    assert bad_session.get.called  # warm-up 尝试
    assert session_after_login.get.called  # 登录后再 warm-up


def test_fetch_icourse_returns_empty_when_no_courses(monkeypatch, fake_session):
    """用户没有任何已注册课程时应返回空列表（不报错）。"""
    monkeypatch.setattr(dc, "_icourse_get_my_courses", lambda s: [])
    monkeypatch.setattr(dc, "_icourse_rpc",
                        lambda *a, **kw: pytest.fail("不该调 RPC"))
    items = dc.fetch_icourse({"icourse_cookies": {"NTESSTUDYSI": "x"}})
    assert items == []
