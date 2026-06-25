"""Background polling/booking worker.

Each Job:
- searches the target SRT train at randomized intervals (1~30s)
- when a seat opens, reserves
- if mode=auto: pays immediately with stored card
- if mode=manual: stops and waits for user "결제 진행" command
"""
from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Deque, Optional

from SRT import SRT, Adult, SeatType
from SRT.errors import SRTError, SRTNotLoggedInError, SRTNetFunnelError

import config
from recovery import RecoveryController

MIN_INTERVAL = 1.0
MAX_INTERVAL = 30.0
# 세션을 만료 전에 미리 갱신해 만료발(發) 오류를 예방한다(선제 재로그인).
SESSION_MAX_AGE = 600.0
# 정상 검색이 이 시간 이상 끊기면 세션이 꼬인 것으로 보고 강제로 새 세션을 만든다.
STALL_LIMIT = 240.0
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


@dataclass
class JobSpec:
    dep: str
    arr: str
    date: str  # YYYYMMDD
    time: str  # HHMMSS
    train_number: Optional[str]  # if set, only match this train
    passengers: int
    seat_pref: str  # "general" | "special" | "any"
    pay_mode: PayMode


@dataclass
class Job:
    id: str
    spec: JobSpec
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    attempts: int = 0
    recoveries: int = 0
    reservation_summary: Optional[str] = None
    payment_deadline: Optional[str] = None
    error: Optional[str] = None
    logs: Deque[str] = field(default_factory=lambda: deque(maxlen=LOG_LIMIT))
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None
    _reservation: object = None  # SRTReservation when reserved
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
            jid = f"j{self._counter}"
        job = Job(id=jid, spec=spec)
        self._jobs[jid] = job
        t = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"srt-{jid}")
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
        creds = config.srt.load()
        if not creds:
            job.status = JobStatus.ERROR
            job.error = "credentials not configured"
            job.log("ERROR: credentials missing")
            return

        def _new_client() -> SRT:
            return SRT(creds.srt_id, creds.srt_password)

        try:
            srt = _new_client()
            session_started = time.monotonic()
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"login failed: {e}"
            job.log(f"login failed: {e}")
            return

        job.log(f"login ok ({creds.srt_id}); polling {job.spec.dep}->{job.spec.arr} {job.spec.date} {job.spec.time}")
        job.status = JobStatus.POLLING

        seat_choice = self._seat_pref_to_enum(job.spec.seat_pref)
        rc = RecoveryController()
        last_ok = time.monotonic()

        def _handle_netfunnel(e: Exception) -> float:
            """NetFunnel 차단(gRtype=4999 등) 처리. 캐시 키를 무효화하고,
            연속 실패에 비례한 백오프 대기시간을 돌려준다. 빠른 재시도는 차단을
            연장하므로 절대 즉시 재시도하지 않는다. 일정 횟수마다 새 세션."""
            nonlocal srt, session_started
            helper = getattr(srt, "netfunnel_helper", None)
            if helper is not None:
                helper._cached_key = None  # 오염된 키 폐기
            rec = rc.on_error()
            job.recoveries += 1
            if rec.fresh_login:
                job.log(
                    f"netfunnel 차단 {rec.streak}회 연속 → 완전 새 세션 + {rec.sleep:.0f}s 대기"
                )
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                except Exception as e2:
                    job.log(f"새 세션 실패(대기 후 재시도): {e2}")
            else:
                job.log(
                    f"netfunnel 차단 #{rec.streak} (IP 속도제한) → "
                    f"{rec.sleep:.0f}s 백오프 후 재시도: {str(e)[:60]}"
                )
            return rec.sleep

        while not job._stop.is_set():
            job.attempts += 1
            next_sleep: Optional[float] = None

            # 선제 세션 갱신: 오래된 세션은 만료로 오류 나기 전에 미리 새로 로그인
            if time.monotonic() - session_started > SESSION_MAX_AGE:
                job.log("세션 선제 갱신(만료 예방) → 재로그인")
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                except Exception as e:
                    job.log(f"선제 재로그인 실패: {e}")

            try:
                trains = srt.search_train(
                    job.spec.dep, job.spec.arr, job.spec.date, job.spec.time,
                    available_only=False,
                )
                rc.on_success()
                last_ok = time.monotonic()
                target = self._pick_target(trains, job.spec)
                if target is None:
                    job.log(f"#{job.attempts} target not found")
                else:
                    gen = target.general_seat_available()
                    spc = target.special_seat_available()
                    job.log(f"#{job.attempts} {target.train_number} general={gen} special={spc}")
                    if self._can_take(gen, spc, job.spec.seat_pref):
                        seat = self._reserve_seat(gen, spc, seat_choice)
                        passengers = [Adult(job.spec.passengers)]
                        try:
                            res = srt.reserve(target, passengers=passengers, special_seat=seat)
                        except SRTError as e:
                            # raced with another buyer; keep polling
                            job.log(f"reserve race lost: {e}")
                        else:
                            job._reservation = res
                            job.reservation_summary = str(res)
                            job.payment_deadline = (
                                f"{getattr(res, 'payment_date', '?')} {getattr(res, 'payment_time', '')}".strip()
                            )
                            job.status = JobStatus.RESERVED
                            job.log(f"RESERVED: {res}")
                            job.log(f"deadline: {job.payment_deadline}")
                            self._handle_payment(srt, job, creds)
                            return
            except SRTNotLoggedInError:
                job.log("세션 만료 감지 → 재로그인")
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"재로그인 실패: {e}")
            except SRTNetFunnelError as e:
                next_sleep = _handle_netfunnel(e)
            except Exception as e:
                if "NetFunnel" in str(e):
                    next_sleep = _handle_netfunnel(e)
                else:
                    job.log(f"poll error: {e}")

            # 정상 검색이 너무 오래 끊기면 세션이 꼬인 것 → 강제로 새 세션
            if next_sleep is None and time.monotonic() - last_ok > STALL_LIMIT:
                job.log(f"검색 {int(STALL_LIMIT)}s+ 정체 → 강제 새 세션")
                job.recoveries += 1
                try:
                    srt = _new_client()
                    session_started = time.monotonic()
                    last_ok = time.monotonic()
                    rc.on_success()
                except Exception as e:
                    job.log(f"강제 재로그인 실패: {e}")

            sleep_for = next_sleep if next_sleep is not None else random.uniform(MIN_INTERVAL, MAX_INTERVAL)
            job.log(f"sleep {sleep_for:.1f}s")
            if job._stop.wait(sleep_for):
                break

        if job.status == JobStatus.POLLING:
            job.status = JobStatus.STOPPED
            job.log("stopped")

    def _handle_payment(self, srt: SRT, job: Job, creds: config.SRTCredentials) -> None:
        if job.spec.pay_mode == PayMode.AUTO:
            job.log("auto-pay enabled, charging card now")
            self._pay(srt, job, creds)
            return

        job.log("manual mode: waiting for user '결제 진행' command (or stop)")
        # wait up to 9 minutes (SRT gives ~10 min, leave a margin)
        if job._pay_event.wait(timeout=540):
            if job._stop.is_set():
                job.log("stopped before payment")
                return
            job.log("user confirmed, charging card now")
            self._pay(srt, job, creds)
        else:
            job.status = JobStatus.ERROR
            job.error = "payment confirmation timeout"
            job.log("ERROR: payment confirmation timeout (~9min); reservation likely auto-cancelled by SRT")

    def _pay(self, srt: SRT, job: Job, creds: config.SRTCredentials) -> None:
        try:
            ok = srt.pay_with_card(
                job._reservation,
                number=creds.card_number,
                password=creds.card_password,
                validation_number=creds.card_validation,
                expire_date=creds.card_expire,
                installment=creds.card_installment,
                card_type=creds.card_type,
            )
            if ok:
                job.status = JobStatus.PAID
                job.log("PAID OK")
            else:
                job.status = JobStatus.ERROR
                job.error = "pay_with_card returned False"
                job.log("ERROR: pay_with_card returned False")
        except Exception as e:
            job.status = JobStatus.ERROR
            job.error = f"payment error: {e}"
            job.log(f"ERROR: payment failed: {e}")

    @staticmethod
    def _pick_target(trains, spec: JobSpec):
        if spec.train_number:
            for t in trains:
                if t.train_number == spec.train_number:
                    return t
            return None
        # else first train at/after the requested time
        return trains[0] if trains else None

    @staticmethod
    def _can_take(gen: bool, spc: bool, pref: str) -> bool:
        if pref == "general":
            return gen
        if pref == "special":
            return spc
        return gen or spc

    @staticmethod
    def _seat_pref_to_enum(pref: str) -> SeatType:
        if pref == "special":
            return SeatType.SPECIAL_FIRST
        if pref == "general":
            return SeatType.GENERAL_FIRST
        return SeatType.GENERAL_FIRST

    @staticmethod
    def _reserve_seat(gen: bool, spc: bool, fallback: SeatType) -> SeatType:
        if fallback == SeatType.GENERAL_FIRST and gen:
            return SeatType.GENERAL_FIRST
        if fallback == SeatType.SPECIAL_FIRST and spc:
            return SeatType.SPECIAL_FIRST
        # any-mode or fallback: pick whichever is open
        if gen and not spc:
            return SeatType.GENERAL_FIRST
        if spc and not gen:
            return SeatType.SPECIAL_FIRST
        return SeatType.GENERAL_FIRST


manager = JobManager()


def search_preview(dep: str, arr: str, date: str, time_: str) -> list[dict]:
    creds = config.srt.load()
    if not creds:
        raise RuntimeError("credentials not configured")
    srt = SRT(creds.srt_id, creds.srt_password)
    # SRTrain's session has no timeout by default; force one so a hanging
    # NetFunnel call can't lock the search endpoint forever.
    _force_session_timeout(srt._session, 25)
    if hasattr(srt, "netfunnel_helper") and hasattr(srt.netfunnel_helper, "session"):
        _force_session_timeout(srt.netfunnel_helper.session, 25)
    trains = srt.search_train(dep, arr, date, time_, available_only=False)
    out = []
    for t in trains[:25]:
        out.append({
            "train_number": t.train_number,
            "label": str(t),
            "general": t.general_seat_available(),
            "special": t.special_seat_available(),
        })
    return out


def _force_session_timeout(session, seconds: float) -> None:
    """Wrap session.request so every HTTP call has a default timeout."""
    if getattr(session, "_kt_timeout_patched", False):
        return
    orig = session.request
    def request(method, url, **kw):
        kw.setdefault("timeout", seconds)
        return orig(method, url, **kw)
    session.request = request
    session._kt_timeout_patched = True
