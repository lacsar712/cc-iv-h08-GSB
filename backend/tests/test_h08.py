"""H08 回归：只读账号提交必须被接口拒收（403、不落盘、无空行伪成功），
只有扫描员的提交真正写库后才返回 201；两条种子数据始终不受牵连。

行为测试在容器内（含 litestar/https/jose/passlib）直接运行，数据库以
内存假连接替身，不依赖真实 PostgreSQL：

    pytest backend/tests/test_h08.py
"""
import importlib
import re
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
FRONTEND_APP = BACKEND.parent / "frontend" / "src" / "App.vue"


class _Result:
    def __init__(self, rows=None):
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Conn:
    """最小内存连接：只实现 api.py 用到的 execute/commit/上下文协议。"""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        pass

    def execute(self, sql, params=None):
        params = list(params or ())
        head = sql.lstrip().upper()
        if head.startswith("CREATE") or head.startswith("DROP"):
            return _Result()
        if "COUNT(*)" in sql.upper():
            return _Result([{"n": len(self.store["rows"])}])
        if "INSERT INTO IV_SCANS" in sql.upper():
            return _Result([self._insert(sql, params)])
        if "SELECT" in sql.upper() and "FROM IV_SCANS" in sql.upper():
            rows = sorted(self.store["rows"], key=lambda r: r["id"], reverse=True)
            return _Result([dict(r) for r in rows])
        raise AssertionError(f"未预期的 SQL: {sql[:60]}")

    def _insert(self, sql, params):
        cols = [c.strip() for c in re.search(
            r"INSERT INTO iv_scans\s*\(([^)]+)\)", sql, re.I | re.S
        ).group(1).split(",")]
        tokens = [t.strip() for t in re.search(
            r"VALUES\s*\(([^)]+)\)", sql, re.I | re.S
        ).group(1).split(",")]
        values = []
        for tok in tokens:
            if tok == "%s":
                values.append(params.pop(0))
            elif tok.startswith("'"):
                values.append(tok.strip("'"))
            else:
                values.append(tok)
        row = {
            "id": self.store["next_id"],
            "string_code": None,
            "voc_v": None,
            "isc_a": None,
            "fill_factor": None,
            "status": "pending",
            "verdict": None,
            "reason": None,
            "created_by": None,
            "created_at": None,
            "processed_at": None,
        }
        row.update(dict(zip(cols, values)))
        self.store["next_id"] += 1
        self.store["rows"].append(row)
        returned = re.search(r"RETURNING\s+(.+)$", sql, re.I | re.S)
        ret_cols = [c.strip() for c in returned.group(1).split(",")] if returned else list(row)
        return {c: row[c] for c in ret_cols}


@pytest.fixture()
def client(monkeypatch):
    import api

    store = {"rows": [], "next_id": 1}
    monkeypatch.setattr(api, "connect", lambda: _Conn(store))
    importlib.reload(api)  # 重跑 seed()，向内存库写入两条种子
    from litestar.testing import TestClient

    with TestClient(app=api.app) as test_client:
        yield test_client, store


def _login(test_client, username, password):
    res = test_client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert res.status_code == 200, res.text
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


PAYLOAD = {
    "string_code": "阵列C-串05",
    "voc_v": 40.5,
    "isc_a": 9.0,
    "fill_factor": 0.77,
}


def test_seed_rows_intact_before_anything(client):
    test_client, store = client
    headers = _login(test_client, "watcher", "watch123456")
    rows = test_client.get("/api/logs", headers=headers).json()
    assert len(rows) == 2
    by_code = {r["string_code"]: r for r in rows}
    a = by_code["阵列A-串03"]
    b = by_code["阵列B-串11"]
    assert (a["fill_factor"], a["verdict"], a["status"], a["created_by"]) == (
        0.78,
        "合格",
        "done",
        "scanner",
    )
    assert (b["fill_factor"], b["verdict"], b["status"]) == (0.61, "衰减", "done")


def test_reader_submit_is_rejected_without_row_or_blank_row(client):
    test_client, store = client
    headers = _login(test_client, "watcher", "watch123456")
    res = test_client.post("/api/logs", headers=headers, json=PAYLOAD)
    assert res.status_code == 403
    assert res.json()["detail"] == "仅扫描员可提交IV扫描"
    # 库行数不变，且没有任何空行/伪行落盘
    assert len(store["rows"]) == 2
    assert all(r["string_code"] for r in store["rows"])
    assert all(r["created_by"] == "scanner" for r in store["rows"])
    # 重复尝试同样被拒，仍无新行
    res = test_client.post("/api/logs", headers=headers, json=PAYLOAD)
    assert res.status_code == 403
    assert len(store["rows"]) == 2


def test_anonymous_submit_is_unauthorized(client):
    test_client, store = client
    res = test_client.post("/api/logs", json=PAYLOAD)
    assert res.status_code == 401
    assert len(store["rows"]) == 2


def test_writer_submit_persists_real_row(client):
    test_client, store = client
    headers = _login(test_client, "scanner", "scan123456")
    res = test_client.post("/api/logs", headers=headers, json=PAYLOAD)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] >= 1
    assert body["status"] == "pending"
    assert body["string_code"] == "阵列C-串05"
    assert body["created_by"] == "scanner"
    rows = test_client.get("/api/logs", headers=headers).json()
    assert len(rows) == 3
    assert {r["string_code"] for r in rows} == {
        "阵列A-串03",
        "阵列B-串11",
        "阵列C-串05",
    }


def test_writer_invalid_payload_rejected_without_row(client):
    test_client, store = client
    headers = _login(test_client, "scanner", "scan123456")
    res = test_client.post(
        "/api/logs",
        headers=headers,
        json={"string_code": "  ", "voc_v": 1, "isc_a": 1, "fill_factor": 0.8},
    )
    assert res.status_code == 400
    assert len(store["rows"]) == 2


def test_trap_modules_removed():
    for name in ("false_enqueue.py", "h08_ui_trap.py", "h08_extra_trap.py"):
        assert not (BACKEND / name).exists(), f"{name} 必须删除"
    api_src = (BACKEND / "api.py").read_text(encoding="utf-8")
    assert "h08_extra_trap" not in api_src
    assert "false_enqueue" not in api_src
    assert '"id": 0' not in api_src and "'id': 0" not in api_src


def test_frontend_gates_form_on_real_role_and_success_on_persistence_only():
    src = FRONTEND_APP.read_text(encoding="utf-8")
    # 入口必须按真实角色收起，不能对只读账号恒显
    assert "computed(() => true)" not in src
    assert 'role === "writer"' in src
    assert 'v-if="isWriter"' in src
    # 只有 201 且带回真实行 id 才允许“已入队”成功提示与列表刷新
    assert "res.status !== 201" in src
    assert "已入队" in src
