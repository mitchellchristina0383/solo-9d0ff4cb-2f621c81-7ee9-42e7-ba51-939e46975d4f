"""掉电恢复与持久化测试：写盘生效语义、重启暂停、确认卷位后续作、写盘失败回滚。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.state import STATUS_ACTIVE, STATUS_PAUSED

client = TestClient(app)

DEMO_JOB = {
    "files": ["A", "B", "C", "D", "E", "F"],
    "copies": [
        {"file": "A", "volume": 1, "position": 2},
        {"file": "A", "volume": 2, "position": 5},
        {"file": "B", "volume": 1, "position": 6},
        {"file": "C", "volume": 2, "position": 1},
        {"file": "C", "volume": 3, "position": 4},
        {"file": "D", "volume": 3, "position": 1},
        {"file": "E", "volume": 3, "position": 6},
        {"file": "F", "volume": 1, "position": 9},
        {"file": "F", "volume": 3, "position": 8},
    ],
}


@pytest.fixture(autouse=True)
def reset_job():
    client.delete("/job")
    yield
    client.delete("/job")


def create_demo_job():
    resp = client.post("/jobs", json=DEMO_JOB)
    assert resp.status_code == 201, resp.text
    return resp.json()


def report(file, volume, position, result):
    return client.post(
        "/receipts",
        json={"file": file, "volume": volume, "position": position, "result": result},
    )


def test_state_file_created_and_survives_reload(isolated_store):
    create_demo_job()
    assert isolated_store._path.exists()

    # 回执：A@1:2 失败（停用）、B@1:6 成功
    assert report("A", 1, 2, "failed").status_code == 200
    assert report("B", 1, 6, "ok").status_code == 200

    # 模拟掉电重启：丢弃内存，按磁盘快照恢复
    job = isolated_store.reload()
    assert job is not None
    assert job.status == STATUS_PAUSED
    assert job.current_volume == 1 and job.head == 6
    assert job.done == {"B"}
    assert job.reads_ok == 1 and job.reads_failed == 1
    assert job.mounts_done == 1
    assert not job.copies[0].enabled  # A@1:2 坏副本保持停用
    assert len(job.history) == 2

    state = client.get("/state").json()
    assert state["status"] == "paused"
    assert state["done"] == ["B"]
    assert state["pending"] == ["A", "C", "D", "E", "F"]
    assert state["disabled_copies"] == [{"file": "A", "volume": 1, "position": 2}]


def test_paused_job_stops_steps_and_rejects_receipts(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")

    # 通过 store 单例触发 reload（conftest 已替换 main.store），模拟掉电重启
    from app import main as main_module

    main_module.store.reload()

    # 暂停期间仍可查已入账进度
    state = client.get("/state").json()
    assert state["status"] == "paused"
    assert state["done"] == []
    assert state["executed"]["reads_failed"] == 1

    # 停发读带步骤
    plan = client.get("/plan").json()
    assert plan["steps"] == [] and plan["next_step"] is None
    assert plan["unretrievable"] == []

    # 拒收回执
    resp = report("B", 1, 6, "ok")
    assert resp.status_code == 409
    assert "暂停" in resp.json()["detail"]
    # 进度不变
    assert client.get("/state").json()["status"] == "paused"
    assert client.get("/state").json()["head"] == 2


def test_resume_with_measured_volume_and_head_replans(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")
    isolated_store.reload()
    assert client.get("/state").json()["status"] == "paused"

    # 值班员上报：实际仍挂卷1、带头在 6（原计划下一步正是 B@1:6）
    resp = client.post("/resume", json={"volume": 1, "head": 6})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"]["status"] == "active"
    assert body["state"]["current_volume"] == 1 and body["state"]["head"] == 6
    # 已取/坏副本/累计计数保留
    assert body["state"]["executed"]["reads_failed"] == 1
    assert body["state"]["disabled_copies"] == [{"file": "A", "volume": 1, "position": 2}]
    # 按实测位置重排，下一步直接是 B
    assert body["plan"]["next_step"] == {
        "seq": 1, "action": "read", "volume": 1, "file": "B", "position": 6,
    }

    # 续作完整跑完
    for f, v, p, r in [
        ("B", 1, 6, "ok"),
        ("C", 2, 1, "ok"),
        ("A", 2, 5, "failed"),
        ("D", 3, 1, "ok"),
        ("E", 3, 6, "ok"),
        ("F", 3, 8, "ok"),
    ]:
        assert report(f, v, p, r).status_code == 200
    final = client.get("/state").json()
    assert final["done"] == ["B", "C", "D", "E", "F"]
    assert final["unretrievable"] == ["A"]
    # 掉电前已计 1 次装带（卷1），续作再装卷2、卷3
    assert final["executed"]["mounts"] == 3
    assert final["executed"]["reads_failed"] == 2


def test_resume_can_confirm_empty_machine(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")
    isolated_store.reload()

    # 确认空机：volume=null, head=0
    resp = client.post("/resume", json={"volume": None, "head": 0})
    assert resp.status_code == 200
    state = resp.json()["state"]
    assert state["status"] == "active"
    assert state["current_volume"] is None and state["head"] == 0
    # 空机重排：需要重新装带，计划从装带开始
    plan = resp.json()["plan"]
    assert plan["steps"][0]["action"] == "mount"
    assert plan["next_step"]["file"] == "B"


def test_resume_empty_machine_rejects_nonzero_head(isolated_store):
    create_demo_job()
    isolated_store.reload()
    resp = client.post("/resume", json={"volume": None, "head": 3})
    assert resp.status_code == 422


def test_resume_while_active_conflicts(isolated_store):
    create_demo_job()  # 新建作业是 active
    resp = client.post("/resume", json={"volume": 1, "head": 0})
    assert resp.status_code == 409


def test_resume_without_job_is_404(isolated_store):
    client.delete("/job")
    resp = client.post("/resume", json={"volume": 1, "head": 0})
    assert resp.status_code == 404


def test_write_fault_on_receipt_leaves_record_untouched(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")
    before = client.get("/state").json()

    # 注入一次写盘故障：回执应被拒绝（500），内存与磁盘记录都保持原样
    isolated_store.inject_write_fault(1)
    resp = report("B", 1, 6, "ok")
    assert resp.status_code == 500
    assert "写盘失败" in resp.json()["detail"]

    after = client.get("/state").json()
    assert after["head"] == before["head"] == 2
    assert after["done"] == before["done"] == []
    assert after["executed"] == before["executed"]
    assert len(after["history"]) == 1

    # 模拟重启：恢复的仍是最后一次写盘成功的记录（只有 A 失败那一步）
    job = isolated_store.reload()
    assert job.head == 2 and job.reads_failed == 1 and job.reads_ok == 0
    assert job.status == STATUS_PAUSED

    # 故障只注入一次，恢复确认后续作可正常写盘
    client.post("/resume", json={"volume": 1, "head": 2})
    ok = report("B", 1, 6, "ok")
    assert ok.status_code == 200
    assert client.get("/state").json()["done"] == ["B"]


def test_write_fault_on_create_keeps_old_job(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")

    new_job = {
        "files": ["X"],
        "copies": [{"file": "X", "volume": 7, "position": 1}],
    }
    isolated_store.inject_write_fault(1)
    resp = client.post("/jobs", json=new_job)
    assert resp.status_code == 500

    # 旧作业原样保留
    state = client.get("/state").json()
    assert state["pending"] == ["A", "B", "C", "D", "E", "F"]
    assert state["head"] == 2
    # 重启后仍是旧作业且暂停
    job = isolated_store.reload()
    assert job.files == list("ABCDEF")
    assert job.status == STATUS_PAUSED


def test_write_fault_on_reset_keeps_job(isolated_store):
    create_demo_job()
    isolated_store.inject_write_fault(1)
    resp = client.delete("/job")
    assert resp.status_code == 500
    # 作业仍在
    assert client.get("/state").status_code == 200
    job = isolated_store.reload()
    assert job is not None and job.files == list("ABCDEF")

    # 无故障时作废生效，重启按无作业处理
    assert client.delete("/job").status_code == 204
    assert isolated_store.reload() is None
    assert client.get("/state").status_code == 404


def test_write_fault_on_resume_keeps_job_paused(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")
    isolated_store.reload()
    assert client.get("/state").json()["status"] == "paused"

    isolated_store.inject_write_fault(1)
    resp = client.post("/resume", json={"volume": 1, "head": 6})
    assert resp.status_code == 500
    state = client.get("/state").json()
    assert state["status"] == "paused"
    # 卷位也未被改动
    assert state["current_volume"] == 1 and state["head"] == 2

    # 再确认一次（无故障）即可恢复
    resp = client.post("/resume", json={"volume": 1, "head": 6})
    assert resp.status_code == 200
    assert resp.json()["state"]["status"] == "active"


def test_completed_job_reloads_active_without_pause(isolated_store):
    # 单卷两文件、全部成功：无待取文件时重启不再暂停
    job = {
        "files": ["X", "Y"],
        "copies": [
            {"file": "X", "volume": 1, "position": 1},
            {"file": "Y", "volume": 1, "position": 2},
        ],
    }
    client.post("/jobs", json=job)
    assert report("X", 1, 1, "ok").status_code == 200
    assert report("Y", 1, 2, "ok").status_code == 200

    job_state = isolated_store.reload()
    assert job_state.pending() == []
    assert job_state.status == STATUS_ACTIVE
    state = client.get("/state").json()
    assert state["status"] == "active"
    assert state["done"] == ["X", "Y"]
    # 无步骤可发，但这不是暂停态：/resume 应返回 409
    assert client.get("/plan").json()["next_step"] is None
    assert client.post("/resume", json={"volume": 1, "head": 2}).status_code == 409


def test_unretrievable_files_still_count_as_pending_and_pause(isolated_store):
    create_demo_job()
    # A 两个副本都失败，其余成功，计划清空但 A 仍在待取（无副本可读）
    for f, v, p, r in [
        ("A", 1, 2, "failed"),
        ("B", 1, 6, "ok"),
        ("C", 2, 1, "ok"),
        ("A", 2, 5, "failed"),
        ("D", 3, 1, "ok"),
        ("E", 3, 6, "ok"),
        ("F", 3, 8, "ok"),
    ]:
        report(f, v, p, r)
    job = isolated_store.reload()
    assert job.pending() == ["A"]
    assert job.status == STATUS_PAUSED  # 有待取文件（即使已无副本）仍暂停
    state = client.get("/state").json()
    assert state["status"] == "paused"
    assert state["unretrievable"] == ["A"]
    # 确认空机续作：计划仍无步骤，但无副本清单保留
    resp = client.post("/resume", json={"volume": None, "head": 0})
    assert resp.status_code == 200
    assert resp.json()["plan"]["next_step"] is None
    assert resp.json()["plan"]["unretrievable"] == ["A"]


def test_pause_survives_multiple_restarts_until_confirmation(isolated_store):
    create_demo_job()
    report("A", 1, 2, "failed")
    isolated_store.reload()
    isolated_store.reload()
    assert client.get("/state").json()["status"] == "paused"

    client.post("/resume", json={"volume": 2, "head": 0})
    # 确认后再次重启：不再自动暂停（最后写盘成功的状态已是 active，且仍有待取）
    job = isolated_store.reload()
    assert job.status == STATUS_PAUSED  # 有待取文件，重启仍暂停
    # 但已入账的卷位是确认后的实测值
    assert job.current_volume == 2 and job.head == 0
