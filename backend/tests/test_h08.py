import importlib.util
import sys
import types
from types import SimpleNamespace

import pytest
from jose import jwt
from litestar.exceptions import HTTPException

from auth import SECRET, need_login, need_writer


class FakeResult:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows or []

    def fetchone(self):
        return self._row

    def fetchall(self):
        return self._rows


class FakeConn:
    """内存版连接：记录 INSERT，SELECT COUNT 返回非零以跳过种子。"""

    def __init__(self):
        self.inserted = []
        self.next_id = 100

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        head = " ".join(sql.split()).lower()
        if head.startswith("select count(*)"):
            return FakeResult(row={"n": 2})
        if head.startswith("insert into iv_scans"):
            self.next_id += 1
            row = {
                "id": self.next_id,
                "string_code": params[0],
                "voc_v": params[1],
                "isc_a": params[2],
                "fill_factor": params[3],
                "status": "pending",
                "verdict": None,
                "reason": None,
                "created_by": params[4],
                "created_at": params[5],
                "processed_at": None,
            }
            self.inserted.append(row)
            return FakeResult(row=row)
        if head.startswith("select id, string_code"):
            return FakeResult(rows=list(reversed(self.inserted)))
        return FakeResult()

    def commit(self):
        pass


fake_conn = FakeConn()
fake_db = types.ModuleType("db")
fake_db.SCHEMA = ""
fake_db.DSN = ""
fake_db.connect = lambda: fake_conn
sys.modules["db"] = fake_db

import api  # noqa: E402  必须在 stub db 之后导入
from litestar.testing import TestClient  # noqa: E402


def make_request(token=None):
    headers = {}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    return SimpleNamespace(headers=headers)


def token_for(username, role):
    return jwt.encode({"sub": username, "role": role}, SECRET, algorithm="HS256")


def auth_header(username, role):
    return {"Authorization": f"Bearer {token_for(username, role)}"}


def scan_payload(code="阵列C-串05"):
    return {"string_code": code, "voc_v": 40.5, "isc_a": 9.0, "fill_factor": 0.75}


def test_writer_passes_need_writer():
    req = make_request(token_for("scanner", "writer"))
    user = need_writer(req)
    assert user["username"] == "scanner"
    assert user["role"] == "writer"


def test_reader_rejected_with_403_and_reason():
    req = make_request(token_for("watcher", "reader"))
    with pytest.raises(HTTPException) as exc:
        need_writer(req)
    assert exc.value.status_code == 403
    assert exc.value.detail == "仅扫描员可提交IV扫描"


def test_reader_gets_no_fake_row():
    req = make_request(token_for("watcher", "reader"))
    try:
        result = need_writer(req)
    except HTTPException:
        return
    pytest.fail(f"旁观者提交未被拒收，反而拿到结果: {result!r}")


def test_anonymous_rejected_with_401():
    with pytest.raises(HTTPException) as exc:
        need_login(make_request())
    assert exc.value.status_code == 401


def test_garbage_token_rejected_with_401():
    with pytest.raises(HTTPException) as exc:
        need_login(make_request("not-a-real-token"))
    assert exc.value.status_code == 401


def test_false_enqueue_traps_removed():
    for mod in ("false_enqueue", "h08_ui_trap", "h08_extra_trap"):
        assert importlib.util.find_spec(mod) is None, f"陷阱模块 {mod} 仍在"


def test_login_returns_writer_role_for_scanner():
    with TestClient(app=api.app) as client:
        res = client.post(
            "/api/auth/login",
            json={"username": "scanner", "password": "scan123456"},
        )
    assert res.status_code == 201 or res.status_code == 200
    data = res.json()
    assert data["role"] == "writer"
    assert data["access_token"]


def test_watcher_post_rejected_and_library_untouched():
    before = len(fake_conn.inserted)
    with TestClient(app=api.app) as client:
        res = client.post(
            "/api/logs", json=scan_payload(), headers=auth_header("watcher", "reader")
        )
    assert res.status_code == 403
    assert res.json()["detail"] == "仅扫描员可提交IV扫描"
    assert len(fake_conn.inserted) == before, "拒收后库中不得新增行"


def test_anonymous_post_rejected_with_401():
    before = len(fake_conn.inserted)
    with TestClient(app=api.app) as client:
        res = client.post("/api/logs", json=scan_payload())
    assert res.status_code == 401
    assert len(fake_conn.inserted) == before


def test_scanner_post_persists_and_returns_real_row():
    before = len(fake_conn.inserted)
    with TestClient(app=api.app) as client:
        res = client.post(
            "/api/logs", json=scan_payload(), headers=auth_header("scanner", "writer")
        )
    assert res.status_code == 201
    data = res.json()
    assert data["id"] > 0, "落盘行必须带真实自增 id"
    assert data["string_code"] == "阵列C-串05"
    assert data["status"] == "pending"
    assert data["created_by"] == "scanner"
    assert len(fake_conn.inserted) == before + 1, "扫描员提交必须真正落盘一行"


def test_seed_rows_left_untouched():
    # 桩连接里 SELECT COUNT 返回非零，seed() 不应写入任何种子行；
    # 真实库中的 阵列A-串03 / 阵列B-串11 种子逻辑未被改动。
    seeds = [r for r in fake_conn.inserted if r["created_by"] == "scanner"
             and r["string_code"] in ("阵列A-串03", "阵列B-串11")]
    assert seeds == []
