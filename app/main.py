"""FastAPI 入口：模拟磁带机，支撑值班员逐步取回文件。"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.responses import RedirectResponse

from .planner import MAX_VOLUMES, Plan, Step
from .schemas import (
    JobIn,
    JobOut,
    PlanOut,
    ReceiptIn,
    ReceiptOut,
    ResumeIn,
    ResumeOut,
    StateOut,
    StepOut,
)
from .state import (
    CopyRecord,
    Job,
    JobPausedError,
    MismatchError,
    NoJobError,
    NoPendingStepError,
    PersistenceError,
    store,
)

app = FastAPI(
    title="磁带机取回模拟器",
    description=(
        "接收待取文件清单与副本目录（至多 12 卷），给出装带 + 回卷总次数最少的读取顺序。"
        "值班员逐步回报读带结果：失败同样推进带头并停用出错副本；"
        "每步按带头新位置重排剩余文件，并列出已无副本可读的文件。"
        "只有当前计划第一步的回执会推进进度。\n\n"
        "掉电恢复：所有变更写盘成功后才生效；重启后若仍有待取文件，作业先暂停，"
        "停发读带步骤并拒收回执，值班员在 POST /resume 上报实际卷号与带头位置（或确认空机）后，"
        "按实测位置重排并保留已取文件、坏副本与累计计数。"
    ),
    version="1.1.0",
)


# ---------- 序列化 ----------

def _step_out(step: Step) -> StepOut:
    return StepOut(
        seq=step.seq,
        action=step.action,
        volume=step.volume,
        file=step.file,
        position=step.position,
    )


def _plan_out(plan: Plan) -> PlanOut:
    nxt = plan.next_read
    return PlanOut(
        volumes_used=plan.volumes_used,
        mounts=plan.mounts,
        rewinds=plan.rewinds,
        cost=plan.cost,
        next_step=_step_out(nxt) if nxt else None,
        steps=[_step_out(s) for s in plan.steps],
        unretrievable=plan.unretrievable,
    )


def _state_out(job: Job) -> StateOut:
    return StateOut(
        status=job.status,
        current_volume=job.current_volume,
        head=job.head,
        pending=sorted(job.pending()),
        done=sorted(job.done),
        unretrievable=job.unretrievable(),
        disabled_copies=[
            {"file": c.file, "volume": c.volume, "position": c.position}
            for c in job.copies
            if not c.enabled
        ],
        executed={
            "mounts": job.mounts_done,
            "rewinds": job.rewinds_done,
            "reads_ok": job.reads_ok,
            "reads_failed": job.reads_failed,
        },
        history=[vars(e) for e in job.history],
    )


def _job_or_404() -> Job:
    try:
        return store.get()
    except NoJobError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未创建取回作业，请先 POST /jobs")


def _paused_plan_out(job: Job) -> PlanOut:
    """暂停期间的计划视图：只保留无副本清单，不发任何读带步骤。"""
    return PlanOut(
        volumes_used=[],
        mounts=0,
        rewinds=0,
        cost=0,
        next_step=None,
        steps=[],
        unretrievable=job.unretrievable(),
    )


# ---------- 接口 ----------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse("/docs")


@app.post(
    "/jobs",
    status_code=status.HTTP_201_CREATED,
    response_model=JobOut,
    summary="创建取回作业（覆盖旧作业），写盘成功后生效，返回初始状态与最优计划",
)
def create_job(body: JobIn) -> JobOut:
    files = [f.strip() for f in body.files]
    if any(not f for f in files):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "文件名不能为空")
    if len(set(files)) != len(files):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "文件清单存在重名")

    known = set(files)
    seen: set[tuple[str, int, int]] = set()
    copies: list[CopyRecord] = []
    for c in body.copies:
        if c.file not in known:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"副本指向未知文件: {c.file!r}",
            )
        key = (c.file, c.volume, c.position)
        if key in seen:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"重复副本: file={c.file} volume={c.volume} position={c.position}",
            )
        seen.add(key)
        copies.append(CopyRecord(file=c.file, volume=c.volume, position=c.position))

    volumes = {c.volume for c in copies}
    if len(volumes) > MAX_VOLUMES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"副本目录涉及 {len(volumes)} 卷，超过上限 {MAX_VOLUMES} 卷",
        )

    try:
        job = store.create(files, copies)
    except PersistenceError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"作业创建失败（写盘未成功，旧记录保持原样）: {exc}",
        )
    return JobOut(state=_state_out(job), plan=_plan_out(job.plan()))


@app.get(
    "/state",
    response_model=StateOut,
    summary="查看当前状态（暂停期间仍可查已入账进度：带头、待取/已取/无副本文件等）",
)
def get_state() -> StateOut:
    return _state_out(_job_or_404())


@app.get(
    "/plan",
    response_model=PlanOut,
    summary="按当前带头位置重算最优读取计划（暂停期间停发读带步骤，steps 为空）",
)
def get_plan() -> PlanOut:
    job = _job_or_404()
    if job.status == "paused":
        return _paused_plan_out(job)
    return _plan_out(job.plan())


@app.post(
    "/resume",
    response_model=ResumeOut,
    summary="掉电重启后上报实际卷号与带头位置（或确认空机），确认后续作并重排",
    responses={
        404: {"description": "尚未创建作业（或作业已作废）"},
        409: {"description": "作业不在暂停状态，无需确认"},
        422: {"description": "卷位参数不合法（如空机却带非零带头）"},
    },
)
def post_resume(body: ResumeIn) -> ResumeOut:
    if body.volume is None and body.head != 0:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "确认空机时带头必须为 0（volume 为空只能配 head=0）",
        )
    try:
        job = store.resume(body.volume, body.head)
    except NoJobError:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "尚未创建取回作业（或作业已作废），无需确认卷位"
        )
    except JobPausedError:
        raise HTTPException(status.HTTP_409_CONFLICT, "作业处于运行中，无需确认卷位")
    except PersistenceError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"卷位确认失败（写盘未成功，作业仍处暂停，记录保持原样）: {exc}",
        )
    where = "空机" if body.volume is None else f"卷{body.volume} 带头{body.head}"
    return ResumeOut(
        message=f"已按实测位置（{where}）确认，作业恢复运行并已重排",
        state=_state_out(job),
        plan=_plan_out(job.plan()),
    )


@app.post(
    "/receipts",
    response_model=ReceiptOut,
    summary="回报当前计划第一步的读带结果，并按带头新位置重排剩余文件",
    responses={
        404: {"description": "尚未创建作业"},
        409: {"description": "回执与当前计划第一步不符、没有待回报步骤，或作业处于暂停"},
        500: {"description": "写盘失败：本次回执未入账，记录保持原样"},
    },
)
def post_receipt(body: ReceiptIn) -> ReceiptOut:
    try:
        job, _before, after = store.apply_receipt(
            body.file, body.volume, body.position, body.result
        )
    except NoJobError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "尚未创建取回作业，请先 POST /jobs")
    except JobPausedError:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "作业处于掉电暂停状态：已停发读带步骤并暂停接收回执，"
            "请先 POST /resume 上报实际卷号与带头位置",
        )
    except NoPendingStepError:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "当前计划没有待回报的读带步骤（已全部完成或无副本可读）"
        )
    except MismatchError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "error": "回执与当前计划第一步不符，进度未更新",
                "expected": {
                    "file": exc.expected.file,
                    "volume": exc.expected.volume,
                    "position": exc.expected.position,
                },
            },
        )
    except PersistenceError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"回执写盘失败，本次回报未入账，作业记录保持原样: {exc}",
        )
    return ReceiptOut(
        applied={
            "file": body.file,
            "volume": body.volume,
            "position": body.position,
            "result": body.result,
            "copy_disabled": body.result == "failed",
        },
        state=_state_out(job),
        plan=_plan_out(after),
    )


@app.delete(
    "/job",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="作废当前作业（写盘成功后生效，重启后仍按无作业处理）",
)
def delete_job() -> Response:
    try:
        store.reset()
    except PersistenceError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            f"作废失败（写盘未成功，作业记录保持原样）: {exc}",
        )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
