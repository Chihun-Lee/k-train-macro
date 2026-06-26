"""Unified FastAPI server for SRT + KTX macros.

- /api/srt/*  → SRT macro (SRTrain, NetFunnel recovery)
- /api/ktx/*  → KTX macro (srtgo + Dynapath bypass, anti-bot recovery)
- Both run independently in the same process; jobs from each side
  share nothing (separate JobManagers, separate Keychain entries).

Listens on 127.0.0.1:8912 (separate from the standalone 8910/8911).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

import card_test
import config
import srt_worker
import ktx_worker

# PyInstaller onefile로 묶이면 정적 파일은 임시 추출 경로(_MEIPASS)에 풀린다.
ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
app = FastAPI(title="K-Rail Macro (SRT + KTX, 개인용)")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


# ─── SRT routes ─────────────────────────────────────────────────────────
srt_router = APIRouter(prefix="/api/srt")


class SRTCredsIn(BaseModel):
    srt_id: str
    srt_password: str
    card_number: str
    card_password: str
    card_validation: str
    card_expire: str
    card_type: str = "J"
    card_installment: int = 0


class SRTSearchIn(BaseModel):
    dep: str
    arr: str
    date: str
    time: str


class SRTJobIn(BaseModel):
    dep: str
    arr: str
    date: str = Field(pattern=r"^\d{8}$")
    time: str = Field(pattern=r"^\d{6}$")
    train_number: Optional[str] = None
    passengers: int = Field(ge=1, le=9, default=1)
    seat_pref: str = Field(default="general", pattern="^(general|special|any)$")
    pay_mode: str = Field(default="manual", pattern="^(auto|manual)$")


@srt_router.get("/config/status")
def srt_config_status():
    return config.srt.public_status()


@srt_router.post("/config")
def srt_config_save(body: SRTCredsIn):
    try:
        creds = config.SRTCredentials(
            srt_id=body.srt_id,
            srt_password=body.srt_password,
            card_number=body.card_number.replace("-", "").replace(" ", ""),
            card_password=body.card_password,
            card_validation=body.card_validation,
            card_expire=body.card_expire,
            card_type=body.card_type,
            card_installment=body.card_installment,
        )
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        raise HTTPException(status_code=422, detail="; ".join(msgs))
    config.srt.save(creds)
    out = config.srt.public_status()
    out["login_ok"], out["login_error"] = _srt_login_test(creds)
    return out


@srt_router.delete("/config")
def srt_config_delete():
    config.srt.clear()
    return {"ok": True}


@srt_router.get("/config/edit")
def srt_config_edit():
    c = config.srt.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    return {
        "srt_id": c.srt_id,
        "card_number": c.card_number,
        "card_validation": c.card_validation,
        "card_expire": c.card_expire,
        "card_type": c.card_type,
        "card_installment": c.card_installment,
    }


def _srt_login_test(creds: config.SRTCredentials) -> tuple[bool, Optional[str]]:
    from SRT import SRT
    try:
        SRT(creds.srt_id, creds.srt_password)
        return True, None
    except Exception as e:
        return False, str(e)[:200]


@srt_router.post("/config/test")
def srt_config_test():
    c = config.srt.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    ok, err = _srt_login_test(c)
    return {"login_ok": ok, "login_error": err}


@srt_router.post("/config/card-test")
def srt_card_test():
    raise HTTPException(
        status_code=501,
        detail=(
            "SRT 카드 테스트는 영구 비활성화됨. "
            "SRT 서버의 reserve_info endpoint가 referer를 무시하고 항상 "
            "다른 결제완료 표 정보를 반환하는 설계라, 자동 환불을 안전하게 "
            "구현할 수 없음. 결제 카드 검증은 SRT 앱에서 수동으로 진행하세요."
        ),
    )


@srt_router.post("/search")
def srt_search(body: SRTSearchIn):
    try:
        return {"trains": srt_worker.search_preview(body.dep, body.arr, body.date, body.time)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"SRT 조회 실패: {_safe_err(e)}")


def _safe_err(e: Exception) -> str:
    """Some exception classes (e.g. requests.ConnectTimeout) have buggy
    __str__ that raises TypeError. Use repr as a guaranteed string."""
    try:
        s = str(e)
        if not isinstance(s, str):
            raise TypeError
        return s or f"{type(e).__name__}"
    except Exception:
        return f"{type(e).__name__}: {e!r}"


def _srt_to_dict(j: srt_worker.Job) -> dict:
    return {
        "id": j.id, "status": j.status,
        "spec": {
            "dep": j.spec.dep, "arr": j.spec.arr,
            "date": j.spec.date, "time": j.spec.time,
            "train_number": j.spec.train_number,
            "passengers": j.spec.passengers,
            "seat_pref": j.spec.seat_pref,
            "pay_mode": j.spec.pay_mode,
        },
        "created_at": j.created_at,
        "attempts": j.attempts,
        "recoveries": j.recoveries,
        "reservation": j.reservation_summary,
        "payment_deadline": j.payment_deadline,
        "error": j.error,
    }


@srt_router.get("/jobs")
def srt_jobs_list():
    return {"jobs": [_srt_to_dict(j) for j in srt_worker.manager.list()]}


@srt_router.post("/jobs")
def srt_jobs_create(body: SRTJobIn):
    if not config.srt.exists():
        raise HTTPException(status_code=400, detail="SRT 자격증명을 먼저 저장해주세요")
    spec = srt_worker.JobSpec(
        dep=body.dep, arr=body.arr, date=body.date, time=body.time,
        train_number=body.train_number, passengers=body.passengers,
        seat_pref=body.seat_pref, pay_mode=srt_worker.PayMode(body.pay_mode),
    )
    return _srt_to_dict(srt_worker.manager.create(spec))


@srt_router.delete("/jobs/{job_id}")
def srt_jobs_stop(job_id: str):
    if not srt_worker.manager.stop(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@srt_router.post("/jobs/{job_id}/pay")
def srt_jobs_confirm_pay(job_id: str):
    if not srt_worker.manager.confirm_pay(job_id):
        raise HTTPException(status_code=400, detail="job not in RESERVED state")
    return {"ok": True}


@srt_router.get("/jobs/{job_id}/log")
def srt_jobs_log(job_id: str, since: int = 0):
    job = srt_worker.manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    lines = list(job.logs)
    return {"lines": lines[since:], "next": len(lines), "status": job.status}


# ─── KTX routes ─────────────────────────────────────────────────────────
ktx_router = APIRouter(prefix="/api/ktx")


class KTXCredsIn(BaseModel):
    ktx_id: str
    ktx_password: str
    card_number: str = ""
    card_password: str = ""
    card_validation: str = ""
    card_expire: str = ""
    card_installment: int = 0


class KTXSearchIn(BaseModel):
    dep: str
    arr: str
    date: str
    time: str
    train_type: str = "ktx"


class KTXJobIn(BaseModel):
    dep: str
    arr: str
    date: str = Field(pattern=r"^\d{8}$")
    time: str = Field(pattern=r"^\d{6}$")
    train_id: Optional[str] = None
    train_type: str = "ktx"
    passengers: int = Field(ge=1, le=9, default=1)
    seat_pref: str = Field(default="general", pattern="^(general|special|any)$")
    pay_mode: str = Field(default="manual", pattern="^(auto|manual)$")
    include_waiting: bool = False


@ktx_router.get("/config/status")
def ktx_config_status():
    return config.ktx.public_status()


@ktx_router.post("/config")
def ktx_config_save(body: KTXCredsIn):
    try:
        creds = config.KTXCredentials(
            ktx_id=body.ktx_id,
            ktx_password=body.ktx_password,
            card_number=body.card_number.replace("-", "").replace(" ", ""),
            card_password=body.card_password,
            card_validation=body.card_validation,
            card_expire=body.card_expire,
            card_installment=body.card_installment,
        )
    except ValidationError as e:
        msgs = [f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()]
        raise HTTPException(status_code=422, detail="; ".join(msgs))
    config.ktx.save(creds)
    out = config.ktx.public_status()
    out["login_ok"], out["login_error"], out["login_name"] = _ktx_login_test(creds)
    return out


@ktx_router.delete("/config")
def ktx_config_delete():
    config.ktx.clear()
    return {"ok": True}


@ktx_router.get("/config/edit")
def ktx_config_edit():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    return {
        "ktx_id": c.ktx_id,
        "card_number": c.card_number,
        "card_validation": c.card_validation,
        "card_expire": c.card_expire,
        "card_installment": c.card_installment,
    }


def _ktx_login_test(creds: config.KTXCredentials) -> tuple[bool, Optional[str], Optional[str]]:
    from ktx_korail import PatchedKorail
    try:
        c = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
        if c.login():
            return True, None, getattr(c, "name", None)
        return False, "login returned False (잘못된 아이디/비밀번호)", None
    except Exception as e:
        return False, str(e)[:200], None


@ktx_router.post("/config/test")
def ktx_config_test():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    ok, err, name = _ktx_login_test(c)
    return {"login_ok": ok, "login_error": err, "login_name": name}


@ktx_router.post("/config/card-test")
def ktx_card_test():
    c = config.ktx.load()
    if not c:
        raise HTTPException(status_code=404, detail="not configured")
    if not c.card_number:
        raise HTTPException(status_code=400, detail="카드 정보가 없습니다")
    # 카드 테스트 중 예기치 못한 예외는 불투명한 500("인터널 에러") 대신
    # 실제 원인을 화면에 보여줘 진단 가능하게 한다.
    try:
        r = card_test.ktx_card_test()
    except Exception as e:
        detail = _safe_err(e)
        return {
            "ok": False,
            "summary": f"카드 테스트 내부 오류: {detail}",
            "steps": [{"name": "error", "ok": False, "detail": detail}],
        }
    return {"ok": r.ok, "summary": r.summary, "steps": r.steps}


@ktx_router.post("/search")
def ktx_search(body: KTXSearchIn):
    try:
        return {"trains": ktx_worker.search_preview(body.dep, body.arr, body.date, body.time, body.train_type)}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=_safe_err(e))
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"KTX 조회 실패: {_safe_err(e)}")


def _ktx_to_dict(j: ktx_worker.Job) -> dict:
    return {
        "id": j.id, "status": j.status,
        "spec": {
            "dep": j.spec.dep, "arr": j.spec.arr,
            "date": j.spec.date, "time": j.spec.time,
            "train_id": j.spec.train_id,
            "train_type": j.spec.train_type,
            "passengers": j.spec.passengers,
            "seat_pref": j.spec.seat_pref,
            "pay_mode": j.spec.pay_mode,
            "include_waiting": j.spec.include_waiting,
        },
        "created_at": j.created_at,
        "attempts": j.attempts,
        "recoveries": j.recoveries,
        "reservation": j.reservation_summary,
        "reservation_id": j.reservation_id,
        "payment_deadline": j.payment_deadline,
        "error": j.error,
    }


@ktx_router.get("/jobs")
def ktx_jobs_list():
    return {"jobs": [_ktx_to_dict(j) for j in ktx_worker.manager.list()]}


@ktx_router.post("/jobs")
def ktx_jobs_create(body: KTXJobIn):
    if not config.ktx.exists():
        raise HTTPException(status_code=400, detail="KTX 자격증명을 먼저 저장해주세요")
    creds = config.ktx.load()
    if body.pay_mode == "auto" and (not creds or not creds.card_number):
        raise HTTPException(status_code=400, detail="자동 결제 모드는 카드정보 저장이 필요합니다")
    spec = ktx_worker.JobSpec(
        dep=body.dep, arr=body.arr, date=body.date, time=body.time,
        train_id=body.train_id, train_type=body.train_type,
        passengers=body.passengers, seat_pref=body.seat_pref,
        pay_mode=ktx_worker.PayMode(body.pay_mode),
        include_waiting=body.include_waiting,
    )
    return _ktx_to_dict(ktx_worker.manager.create(spec))


@ktx_router.delete("/jobs/{job_id}")
def ktx_jobs_stop(job_id: str):
    if not ktx_worker.manager.stop(job_id):
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True}


@ktx_router.post("/jobs/{job_id}/pay")
def ktx_jobs_confirm_pay(job_id: str):
    if not ktx_worker.manager.confirm_pay(job_id):
        raise HTTPException(status_code=400, detail="job not in RESERVED state")
    return {"ok": True}


@ktx_router.get("/jobs/{job_id}/log")
def ktx_jobs_log(job_id: str, since: int = 0):
    job = ktx_worker.manager.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    lines = list(job.logs)
    return {"lines": lines[since:], "next": len(lines), "status": job.status}


app.include_router(srt_router)
app.include_router(ktx_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8912, reload=False)
