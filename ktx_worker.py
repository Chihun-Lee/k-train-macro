"""KTX/Korail polling/booking/payment worker.

Mirrors srt-macro/srt_worker.py but uses the patched srtgo Korail.
"""
from __future__ import annotations

import random
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Optional

from srtgo.ktx import (
    AdultPassenger,
    KorailError,
    NeedToLoginError,
    NoResultsError,
    ReserveOption,
    SoldOutError,
    TrainType,
)

import config
from ktx_korail import PatchedKorail

MIN_INTERVAL = 1.0
MAX_INTERVAL = 30.0
LOG_LIMIT = 500


class JobStatus(str, Enum):
    PENDING = "pending"
    POLLING = "polling"
    RESERVED = "reserved"
    PAID = "paid"
    STOPPED = "stopped"
    ERROR = "error"


class PayMode(str, Enum):
    AUTO = "auto"
    MANUAL = "manual"


TRAIN_TYPE_MAP = {
    "ktx": TrainType.KTX,
    "itx-saemaeul": TrainType.ITX_SAEMAEUL,
    "mugunghwa": TrainType.MUGUNGHWA,
    "nuriro": TrainType.NURIRO,
    "tonggeun": TrainType.TONGGUEN,
    "itx-cheongchun": TrainType.ITX_CHEONGCHUN,
    "all": TrainType.ALL,
}


def _train_id(t) -> str:
    """Stable selector: train_type + train_no + dep_date."""
    return f"{t.train_type}|{t.train_no}|{t.dep_date}"


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str
    time: str
    train_id: Optional[str]
    train_type: str
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode
    include_waiting: bool = False


@dataclass
class Job:
    id: str
    spec: JobSpec
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    reservation_summary: Optional[str] = None
    reservation_id: Optional[str] = None
    payment_deadline: Optional[str] = None
    error: Optional[str] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _reservation: object = None
    _pay_event: threading.Event = field(default_factory=threading.Event)

    def log(self, msg: str) -> None:
        self.logs.append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def list(self) -> list[Job]:
        with self._lock:
            return list(self._jobs.values())

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def create(self, spec: JobSpec) -> Job:
        with self._lock:
            self._counter += 1
            jid = f"k{self._counter}"
        job = Job(id=jid, spec=spec)
        self._jobs[jid] = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"ktx-{jid}")
        job._thread = t
        t.start()
        return job

    def stop(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job._stop.set()
        job._pay_event.set()
        return True

    def confirm_pay(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status != JobStatus.RESERVED:
            return False
        job._pay_event.set()
        return True

    def _run(self, job: Job) -> None:
        creds = config.ktx.load()
        if not creds:
            job.status = JobStatus.ERROR
            job.error = "credentials not configured"
            job.log("ERROR: credentials missing")
            return

        def _new_client() -> PatchedKorail:
            c = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
            if not c.login():
                raise RuntimeError("login returned False")
            return c

        try:
            client = _new_client()
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"login failed: {e}"
            job.log(f"login failed: {e}")
            return

        job.log(
            f"login ok ({getattr(client, 'name', creds.ktx_id)}); "
            f"polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time} "
            f"type={job.spec.train_type}"
        )
        job.status = JobStatus.POLLING

        seat_option = self._seat_pref_to_option(job.spec.seat_pref)
        train_type = TRAIN_TYPE_MAP.get(job.spec.train_type.lower(), TrainType.KTX)
        passengers = [AdultPassenger(job.spec.passengers)]
        consecutive_antibot_errors = 0

        while not job._stop.is_set():
            job.attempts += 1
            try:
                trains = client.search_train(
                    job.spec.dep, job.spec.arr,
                    job.spec.date, job.spec.time,
                    train_type=train_type,
                    include_no_seats=True,
                    include_waiting_list=job.spec.include_waiting,
                )
                consecutive_antibot_errors = 0
                target = self._pick_target(trains, job.spec)
                if target is None:
                    job.log(f"#{job.attempts} target not found")
                else:
                    gen = target.has_general_seat()
                    spc = target.has_special_seat()
                    job.log(f"#{job.attempts} {target.train_no} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        try:
                            res = client.reserve(target, passengers=passengers, option=seat_option)
                        except SoldOutError:
                            job.log("reserve race lost (sold out)")
                        except KorailError as e:
                            job.log(f"reserve error: {e}")
                        else:
                            job._reservation = res
                            job.reservation_summary = str(res)
                            job.reservation_id = getattr(res, "rsv_id", None)
                            d = getattr(res, "buy_limit_date", None)
                            t = getattr(res, "buy_limit_time", None)
                            if d and t and d != "00000000":
                                job.payment_deadline = (
                                    f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
                                )
                            job.status = JobStatus.RESERVED
                            job.log(f"RESERVED: {res}")
                            job.log(f"deadline: {job.payment_deadline}")
                            self._handle_payment(client, job, creds)
                            return
            except NoResultsError:
                job.log(f"#{job.attempts} no results")
                consecutive_antibot_errors = 0
            except NeedToLoginError:
                job.log("session expired, re-login")
                try:
                    client = _new_client()
                    consecutive_antibot_errors = 0
                except Exception as e:
                    job.log(f"re-login failed: {e}")
            except KorailError as e:
                msg = str(e)
                if any(p in msg for p in ("MACRO", "원활한 서비스", "최신 버전")):
                    consecutive_antibot_errors += 1
                    if consecutive_antibot_errors >= 3:
                        job.log(f"anti-bot persistent ({consecutive_antibot_errors}x) → recreating client")
                        try:
                            client = _new_client()
                            consecutive_antibot_errors = 0
                        except Exception as e2:
                            job.log(f"client recreate failed: {e2}")
                    else:
                        job.log(f"anti-bot error #{consecutive_antibot_errors}: {msg[:80]}")
                else:
                    job.log(f"korail error: {msg[:120]}")
            except Exception as e:
                job.log(f"poll error: {type(e).__name__}: {e}")

            sleep_for = random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            job.log(f"sleep {sleep_for:.1f}s")
            if job._stop.wait(sleep_for):
                break

        if job.status == JobStatus.POLLING:
            job.status = JobStatus.STOPPED
            job.log("stopped")

    def _handle_payment(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> None:
        if job.spec.pay_mode == PayMode.MANUAL:
            job.log("manual mode: waiting for user '결제 진행' (~9min)")
            if job._pay_event.wait(timeout=540):
                if job._stop.is_set():
                    job.log("stopped before payment")
                    return
                job.log("user confirmed → charging card")
                self._pay(client, job, creds)
            else:
                job.status = JobStatus.ERROR
                job.error = "payment confirmation timeout"
                job.log("ERROR: confirm timeout (~9min); reservation likely auto-cancelled")
            return

        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "auto pay requested but card not configured"
            job.log("ERROR: auto pay requires card info")
            return
        job.log("auto-pay → charging card")
        self._pay(client, job, creds)

    def _pay(self, client: PatchedKorail, job: Job, creds: config.KTXCredentials) -> None:
        if not creds.card_number:
            job.status = JobStatus.ERROR
            job.error = "card info missing"
            job.log("ERROR: card info not in keychain")
            return
        try:
            ok = client.pay_with_card(
                job._reservation,
                card_number=creds.card_number,
                card_password=creds.card_password,
                birthday=creds.card_validation,
                card_expire=creds.card_expire,
                installment=creds.card_installment,
                card_type="J",
            )
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"pay error: {e}"
            job.log(f"ERROR: pay error: {e}")
            return
        if ok:
            job.status = JobStatus.PAID
            job.log("PAID OK")
        else:
            job.status = JobStatus.ERROR
            job.error = "pay_with_card returned False"
            job.log("ERROR: pay_with_card returned False")

    @staticmethod
    def _pick_target(trains, spec: JobSpec):
        if spec.train_id:
            for t in trains:
                if _train_id(t) == spec.train_id:
                    return t
            return None
        return trains[0] if trains else None

    @staticmethod
    def _can_take(gen: bool, spc: bool, pref: str) -> bool:
        if pref == "general":
            return gen
        if pref == "special":
            return spc
        return gen or spc

    @staticmethod
    def _seat_pref_to_option(pref: str):
        if pref == "special":
            return ReserveOption.SPECIAL_FIRST
        if pref == "general":
            return ReserveOption.GENERAL_FIRST
        return ReserveOption.GENERAL_FIRST


manager = JobManager()


def _force_session_timeout(session, seconds: float) -> None:
    if getattr(session, "_kt_timeout_patched", False):
        return
    orig = session.request
    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return orig(method, url, **kw)
    session.request = request
    session._kt_timeout_patched = True


def search_preview(dep: str, arr: str, date: str, time_: str, train_type: str = "ktx") -> list[dict]:
    creds = config.ktx.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    client = PatchedKorail(creds.ktx_id, creds.ktx_password, auto_login=False)
    _force_session_timeout(client._session, 25)
    if not client.login():
        raise RuntimeError("login failed")
    tt = TRAIN_TYPE_MAP.get(train_type.lower(), TrainType.KTX)
    try:
        trains = client.search_train(dep, arr, date, time_, train_type=tt, include_no_seats=True)
    except NoResultsError:
        return []
    out = []
    for t in trains[:25]:
        out.append({
            "train_id": _train_id(t),
            "train_no": t.train_no,
            "label": str(t),
            "general": t.has_general_seat(),
            "special": t.has_special_seat(),
        })
    return out
