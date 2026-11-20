#!/usr/bin/env python3
"""掉电重启续作走查：对运行中的服务验证“暂停 → 确认卷位 → 续作”。

前置：服务把状态快照写在 TAPE_STATE_FILE 指向的文件里，
脚本通过“杀掉进程再启动”模拟掉电重启（容器重建场景同构：卷保留、进程重建）。

流程：
1. 创建作业，回报两步（A 读失败、B 成功）后停掉服务进程；
2. 重新启动服务：作业应自动暂停，/plan 不发步骤、/receipts 被拒、/state 可查；
3. POST /resume 上报实测卷位（演示一次空机确认被 422 拒绝，再确认空机）；
4. 续作跑完剩余文件，核对已取文件、坏副本、累计计数。

用法:
  python3 scripts/restart_walkthrough.py [base_url]            # 阶段 1：建作业+两步回执
  python3 scripts/restart_walkthrough.py [base_url] resume     # 阶段 2-4：重启后续作
"""
from __future__ import annotations

import json
import sys

import httpx

args = [a for a in sys.argv[1:] if not a.startswith("http")]
urls = [a for a in sys.argv[1:] if a.startswith("http")]
BASE = urls[0] if urls else "http://127.0.0.1:8000"
PHASE = "resume" if args and args[0] == "resume" else "phase1"

JOB = {
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

SCRIPTED_FAILURES = {("A", 1, 2), ("A", 2, 5)}


def show(title: str, resp: httpx.Response) -> dict:
    print(f"-- {title}: HTTP {resp.status_code}")
    if resp.status_code >= 400:
        print(f"   {json.dumps(resp.json(), ensure_ascii=False)}")
        return resp.json()
    return resp.json()


def phase1(client: httpx.Client) -> int:
    print("== 1. 创建作业并回报两步（A 失败、B 成功），模拟掉电前的进度 ==")
    client.delete("/job")
    body = show("POST /jobs", client.post("/jobs", json=JOB))
    assert body["state"]["status"] == "active"
    r = client.post(
        "/receipts",
        json={"file": "A", "volume": 1, "position": 2, "result": "failed"},
    )
    show("回执 A@1:2 failed", r)
    r = client.post(
        "/receipts",
        json={"file": "B", "volume": 1, "position": 6, "result": "ok"},
    )
    show("回执 B@1:6 ok", r)
    state = client.get("/state").json()
    assert state["head"] == 6 and state["done"] == ["B"]
    print("   掉电前状态:", json.dumps(state["executed"], ensure_ascii=False),
          "停用:", state["disabled_copies"])
    print("\n   现在可以重启服务进程；重启后执行: "
          f"python3 {sys.argv[0]} resume")
    return 0


def resume(client: httpx.Client) -> int:
    print("== 2. 重启后自查暂停语义 ==")
    state = show("GET /state（暂停仍可查）", client.get("/state"))
    if state.get("status") != "paused":
        print("   （作业不在暂停状态；请先运行不带 resume 参数的阶段 1 并重启服务）")
        return 2

    plan = show("GET /plan（应停发步骤）", client.get("/plan"))
    assert plan["steps"] == [] and plan["next_step"] is None
    r = client.post(
        "/receipts",
        json={"file": "C", "volume": 2, "position": 1, "result": "ok"},
    )
    show("暂停期回执（应 409 拒收）", r)
    assert r.status_code == 409

    print("\n== 3. 值班员确认卷位 ==")
    r = client.post("/resume", json={"volume": None, "head": 3})
    show("空机却带非零带头（应 422）", r)
    assert r.status_code == 422
    # 实测：掉电后磁带已卸载，确认空机，从卷首重新调度
    r = client.post("/resume", json={"volume": None, "head": 0})
    body = show("确认空机后续作", r)
    assert r.status_code == 200
    assert body["state"]["status"] == "active"
    assert body["state"]["current_volume"] is None
    # 已取/坏副本/计数保留
    assert body["state"]["done"] == ["B"]
    assert body["state"]["disabled_copies"] == [
        {"file": "A", "volume": 1, "position": 2}
    ]
    assert body["state"]["executed"]["reads_failed"] == 1

    print("\n== 4. 续作到计划清空 ==")
    steps = 0
    while True:
        plan = client.get("/plan").json()
        nxt = plan["next_step"]
        if nxt is None:
            break
        steps += 1
        key = (nxt["file"], nxt["volume"], nxt["position"])
        result = "failed" if key in SCRIPTED_FAILURES else "ok"
        r = client.post(
            "/receipts",
            json={
                "file": nxt["file"],
                "volume": nxt["volume"],
                "position": nxt["position"],
                "result": result,
            },
        )
        r.raise_for_status()
        print(f"   续作第 {steps} 步: {key} -> {result}")

    final = client.get("/state").json()
    print("\n== 续作结束 ==")
    print("  已取回:", final["done"])
    print("  无副本可读:", final["unretrievable"])
    print("  累计执行:", final["executed"])
    assert final["done"] == ["B", "C", "D", "E", "F"]
    assert final["unretrievable"] == ["A"]
    # 掉电前装带 1 次（卷1）；确认空机后重排发现剩余文件用卷2+卷3 即可覆盖
    # （卷1 上只剩坏副本 A 与已取的 B，无需再装），续作装带 2 次 → 累计 3 次
    assert final["executed"]["mounts"] == 3
    assert final["executed"]["reads_failed"] == 2
    assert final["executed"]["reads_ok"] == 5
    return 0


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10)
    return resume(client) if PHASE == "resume" else phase1(client)


if __name__ == "__main__":
    sys.exit(main())
