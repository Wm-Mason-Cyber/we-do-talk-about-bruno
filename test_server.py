"""
Smoke tests for the Roster API + dashboard.

    pip install -r requirements.txt pytest httpx
    python3 -m pytest test_server.py -v
"""

import os
import tempfile

os.environ["TEACHER_KEY"] = "test-teacher-key"
os.environ["ACCESS_LOG"] = os.path.join(tempfile.mkdtemp(), "access.log")

import re

import pytest
from fastapi.testclient import TestClient

import app as server
import roster_api


@pytest.fixture(autouse=True)
def fresh_roster():
    roster_api.reset_roster()
    yield


@pytest.fixture
def client():
    return TestClient(server.app)


def test_crud_round_trip(client):
    assert [s["name"] for s in client.get("/students").json()] == ["Ada Lovelace", "Grace Hopper"]

    r = client.post("/students", json={"name": "Evan"})
    assert r.status_code == 201 and r.json() == {"id": 3, "name": "Evan"}

    r = client.put("/students/3", json={"name": "Evan C"})
    assert r.status_code == 200 and r.json()["name"] == "Evan C"

    r = client.delete("/students/3")
    assert r.status_code == 200 and r.json() == {"message": "Student with id 3 deleted."}
    assert client.get("/students/3").status_code == 404


@pytest.mark.parametrize("body", [{}, {"nmae": "typo"}, {"name": ""}, {"name": "x" * 41}])
def test_bad_bodies_are_422(client, body):
    assert client.post("/students", json=body).status_code == 422


def test_wrong_method_is_405(client):
    assert client.delete("/students").status_code == 405


def test_roster_cap(client):
    for i in range(roster_api.MAX_STUDENTS - 2):
        client.post("/students", json={"name": f"s{i}"})
    assert client.post("/students", json={"name": "one too many"}).status_code == 507


def test_admin_key(client):
    r = client.get("/admin-only")
    assert r.status_code == 403
    assert r.json() == {"detail": "Forbidden - Invalid or missing API Key"}
    r = client.get("/admin-only", headers={"X-API-Key": "bruno-is-awesome"})
    assert r.status_code == 200



def test_teapot(client):
    for method in ("get", "post"):
        r = getattr(client, method)("/coffee")
        assert r.status_code == 418
        assert "teapot" in r.json()["detail"]
    r = client.get("/tea")
    assert r.status_code == 200

def test_dashboard_records_changes(client):
    client.post("/students", json={"name": "<img src=x onerror=alert(1)>"})
    client.put("/students/1", json={"name": "Ada L"})
    client.delete("/students/2")
    client.get("/students/99")

    events = client.get("/dashboard/state").json()["events"][-4:]
    assert [(e["method"], e["status"]) for e in events] == [
        ("POST", 201), ("PUT", 200), ("DELETE", 200), ("GET", 404)]
    assert events[0]["changes"] == [{"op": "add", "id": 3, "name": "<img src=x onerror=alert(1)>"}]
    assert events[1]["changes"] == [{"op": "rename", "id": 1, "name": "Ada L", "old": "Ada Lovelace"}]
    assert events[2]["changes"] == [{"op": "remove", "id": 2, "name": "Grace Hopper"}]
    assert events[3]["changes"] == []


def test_dashboard_requests_not_recorded(client):
    before = len(client.get("/dashboard/state").json()["events"])
    client.get("/dashboard")
    client.get("/dashboard/state")
    assert len(client.get("/dashboard/state").json()["events"]) == before


def test_reset_needs_teacher_key(client):
    client.post("/students", json={"name": "Evan"})
    assert client.post("/dashboard/reset").status_code == 403
    assert client.post("/dashboard/reset", headers={"X-Teacher-Key": "nope"}).status_code == 403
    assert client.post("/dashboard/reset", headers={"X-Teacher-Key": "test-teacher-key"}).status_code == 200
    state = client.get("/dashboard/state").json()
    assert [s["id"] for s in state["roster"]] == [1, 2]
    assert state["events"] == []
    assert client.post("/students", json={"name": "Next"}).json()["id"] == 3


def test_access_log_combined_format(client):
    client.post("/students", json={"name": "Logged"}, headers={"User-Agent": "bruno-runtime/2.13.2"})
    client.get("/dashboard/state")   # dashboard traffic stays out of the log
    text = client.get("/logs/access").text
    lines = [l for l in text.splitlines() if l]
    post = [l for l in lines if '"POST /students' in l][-1]
    assert re.match(
        r'^\S+ - - \[\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4}\] '
        r'"POST /students HTTP/1.1" 201 \d+ "-" "bruno-runtime/2.13.2"$', post), post
    assert not any("/dashboard" in l for l in lines)
