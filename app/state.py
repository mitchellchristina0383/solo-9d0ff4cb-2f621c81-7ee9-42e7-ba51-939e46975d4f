"""作业状态与持久化：文件清单、副本目录、带头位置，以及回执的应用。

约定：
- 只有“当前计划第一步（第一个 read 动作）”的回执才会被接受并推进进度；
  其余回执一律拒绝（MismatchError），不更新任何状态。
- 无论读带成功还是失败，带头都推进到该步骤的卷内序号；
  若该步骤在另一卷上，说明装带已发生，当前卷随之切换。
- 失败的副本立即停用，不再参与后续规划。

持久化（掉电恢复）：
- 创建/作废作业、应用回执、确认带头位置，都先把整份作业快照原子写盘，
  写盘成功后才让变更生效；写盘失败抛 PersistenceError，内存记录保持原样。
- 重启时从快照恢复最后一次写盘成功的记录；只要仍有待取文件，
  恢复出来的作业一律进入 paused（暂停）状态，等值班员确认实际卷位后再续作。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

from .planner import Copy, Plan, Step, build_plan

# 作业状态：active=正常运行；paused=掉电重启后等待值班员确认卷位
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"


class NoJobError(Exception):
    """尚未创建取回作业。"""


class NoPendingStepError(Exception):
    """当前计划没有可回报的读带步骤（已全部完成或全部无副本可读）。"""


class MismatchError(Exception):
    """回执与当前计划第一步不符。"""

    def __init__(self, expected: Step):
        super().__init__("receipt does not match the first step of the current plan")
        self.expected = expected


class JobPausedError(Exception):
    """作业处于掉电暂停状态：停发读带步骤，并拒绝一切回执/变更。"""


class PersistenceError(Exception):
    """状态写盘失败：本次变更未生效，作业记录保持写盘前的样子。"""


@dataclass
class CopyRecord:
    file: str
    volume: int
    position: int
    enabled: bool = True


@dataclass
class ReceiptEvent:
    seq: int
    file: str
    volume: int
    position: int
    result: str  # "ok" | "failed"


@dataclass
class Job:
    files: list[str]
    copies: list[CopyRecord]
    done: set[str] = field(default_factory=set)
    current_volume: int | None = None  # 当前已挂载的卷（初始为空机）
    head: int = 0  # 带头所在的卷内序号
    mounts_done: int = 0
    rewinds_done: int = 0
    reads_ok: int = 0
    reads_failed: int = 0
    history: list[ReceiptEvent] = field(default_factory=list)
    status: str = STATUS_ACTIVE  # active | paused

    def enabled_copies(self) -> dict[str, list[Copy]]:
        result: dict[str, list[Copy]] = {}
        for c in self.copies:
            if c.enabled:
                result.setdefault(c.file, []).append(Copy(c.file, c.volume, c.position))
        return result

    def pending(self) -> list[str]:
        return [f for f in self.files if f not in self.done]

    def unretrievable(self) -> list[str]:
        enabled = self.enabled_copies()
        return sorted(f for f in self.pending() if not enabled.get(f))

    def plan(self) -> Plan:
        return build_plan(self.pending(), self.enabled_copies(), self.current_volume, self.head)

    # ---------- 快照序列化 ----------

    def to_snapshot(self) -> dict:
        return {
            "files": list(self.files),
            "copies": [vars(c) for c in self.copies],
            "done": sorted(self.done),
            "current_volume": self.current_volume,
            "head": self.head,
            "mounts_done": self.mounts_done,
            "rewinds_done": self.rewinds_done,
            "reads_ok": self.reads_ok,
            "reads_failed": self.reads_failed,
            "history": [vars(e) for e in self.history],
            "status": self.status,
        }

    @classmethod
    def from_snapshot(cls, data: dict) -> "Job":
        return cls(
            files=list(data["files"]),
            copies=[CopyRecord(**c) for c in data["copies"]],
            done=set(data["done"]),
            current_volume=data["current_volume"],
            head=data["head"],
            mounts_done=data["mounts_done"],
            rewinds_done=data["rewinds_done"],
            reads_ok=data["reads_ok"],
            reads_failed=data["reads_failed"],
            history=[ReceiptEvent(**e) for e in data["history"]],
            status=data.get("status", STATUS_ACTIVE),
        )


def _clone(job: Job) -> Job:
    """深拷贝一份作业，用于“先写盘成功、再让变更生效”。"""
    return Job.from_snapshot(job.to_snapshot())


class JobStore:
    """单作业仓库：内存态 + 磁盘快照，支持掉电后续作。

    state_file 指向状态文件：不存在表示从未建过作业；文件内容为 null
    表示作业已作废。所有变更通过“临时文件 + os.replace”原子落盘。
    """

    def __init__(self, state_file: str | os.PathLike[str] | None = None) -> None:
        self._lock = threading.RLock()
        if state_file is None:
            state_file = os.environ.get("TAPE_STATE_FILE", "tape_state.json")
        self._path = Path(state_file)
        self._faults_remaining = 0
        self._job: Job | None = self._load()

    # ---------- 持久化 ----------

    def _load(self) -> Job | None:
        if not self._path.exists():
            return None
        with self._path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if data is None:
            return None
        job = Job.from_snapshot(data)
        # 掉电重启：只要还有待取文件，先暂停作业，等值班员确认实际卷位
        if job.pending():
            job.status = STATUS_PAUSED
        else:
            job.status = STATUS_ACTIVE
        return job

    def _persist(self, job: Job | None) -> None:
        """原子写盘；可注入故障。失败时原文件与内存状态都不受影响。"""
        if self._faults_remaining > 0:
            self._faults_remaining -= 1
            raise PersistenceError(f"模拟写盘失败（状态文件 {self._path}）")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            None if job is None else job.to_snapshot(),
            ensure_ascii=False,
            indent=2,
        )
        fd, tmp_name = tempfile.mkstemp(
            prefix=".tape-state-", suffix=".tmp", dir=str(self._path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except OSError as exc:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise PersistenceError(f"状态写盘失败: {exc}") from exc

    def inject_write_fault(self, times: int = 1) -> None:
        """测试钩子：让接下来 times 次写盘直接失败。"""
        with self._lock:
            self._faults_remaining += times

    def reload(self) -> Job | None:
        """模拟进程重启：丢弃内存态，按磁盘快照重新加载。"""
        with self._lock:
            self._job = self._load()
            return self._job

    # ---------- 查询 ----------

    def get(self) -> Job:
        with self._lock:
            if self._job is None:
                raise NoJobError
            return self._job

    # ---------- 变更（写盘成功后才生效） ----------

    def create(self, files: list[str], copies: list[CopyRecord]) -> Job:
        with self._lock:
            job = Job(files=list(files), copies=copies)
            self._persist(job)
            self._job = job
            return job

    def reset(self) -> bool:
        """作废当前作业；写盘成功后内存也清空，重启后按无作业处理。"""
        with self._lock:
            existed = self._job is not None
            self._persist(None)
            self._job = None
            return existed

    def resume(self, volume: int | None, head: int) -> Job:
        """值班员确认掉电后实际的卷号/带头位置，作业恢复运行并重排。

        volume=None 表示确认空机；空机时带头必须为 0。
        已取文件、停用副本、累计计数、回执历史全部保留，只改卷位。
        """
        with self._lock:
            if self._job is None:
                raise NoJobError
            if self._job.status != STATUS_PAUSED:
                raise JobPausedError("作业不在暂停状态，无需确认卷位")
            candidate = _clone(self._job)
            candidate.current_volume = volume
            candidate.head = head
            candidate.status = STATUS_ACTIVE
            self._persist(candidate)
            self._job = candidate
            return candidate

    def apply_receipt(
        self, file: str, volume: int, position: int, result: str
    ) -> tuple[Job, Plan, Plan]:
        """应用一张回执，返回 (作业, 执行前计划, 重排后计划)。

        暂停期间拒收回执；先在副本上算出变更并写盘，写盘成功才提交。
        """
        with self._lock:
            if self._job is None:
                raise NoJobError
            job = self._job
            if job.status == STATUS_PAUSED:
                raise JobPausedError("作业处于掉电暂停状态，请先 POST /resume 确认实际卷位")
            before = job.plan()
            step = before.next_read
            if step is None:
                raise NoPendingStepError
            if (step.file, step.volume, step.position) != (file, volume, position):
                raise MismatchError(step)

            candidate = _clone(job)

            # 该读带步骤之前的装带/回卷动作随回执一并确认
            for s in before.steps:
                if s.action == "read":
                    break
                if s.action == "mount":
                    candidate.mounts_done += 1
                elif s.action == "rewind":
                    candidate.rewinds_done += 1

            # 成功或失败都推进带头；跨卷步骤意味着装带已发生
            candidate.current_volume = volume
            candidate.head = position

            if result == "ok":
                candidate.done.add(file)
                candidate.reads_ok += 1
            else:
                for c in candidate.copies:
                    if c.file == file and c.volume == volume and c.position == position:
                        c.enabled = False  # 停用出错的副本
                        break
                candidate.reads_failed += 1

            candidate.history.append(
                ReceiptEvent(
                    seq=len(candidate.history) + 1,
                    file=file,
                    volume=volume,
                    position=position,
                    result=result,
                )
            )

            self._persist(candidate)
            self._job = candidate
            return candidate, before, candidate.plan()


store = JobStore()
