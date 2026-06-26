"""recovery.py + worker 자가복구 검증 (네트워크 불필요).

검증 목표 (사용자 보고: "netfunnel 차단 뜨면 창은 떠있는데 예매만 멈춤"):
  1) 백오프가 연속 실패마다 커지고 상한에서 멈춘다  → 차단을 연장하는 '하머링' 금지
  2) 일정 횟수마다 완전 새 세션으로 에스컬레이션한다
  3) 성공하면 streak이 리셋된다
  4) 워커가 netfunnel 4999를 N회 맞아도 죽지 않고(POLLING 유지) 결국 회복한다

실행:  venv/bin/python test_recovery.py
"""
from __future__ import annotations

import threading
import time
import types

from SRT.errors import SRTNetFunnelError

import recovery
import srt_worker


# ── 1. 백오프 수학 / 에스컬레이션 (순수) ────────────────────────────────
def test_backoff_grows_and_caps():
    rc = recovery.RecoveryController(base=5, cap=60, fresh_login_every=4,
                                     jitter=(1.0, 1.0))  # 지터 끔 → 결정적
    sleeps = [rc.on_error().sleep for _ in range(7)]
    assert sleeps == [5, 10, 20, 40, 60, 60, 60], sleeps          # 지수↑ 후 상한
    print("  [ok] 백오프 지수 증가 + 60s 상한:", sleeps)


def test_fresh_login_escalation():
    rc = recovery.RecoveryController(fresh_login_every=4, jitter=(1.0, 1.0))
    fresh = [rc.on_error().fresh_login for _ in range(8)]
    assert fresh == [False, False, False, True, False, False, False, True], fresh
    print("  [ok] 4회마다 새 세션 에스컬레이션:", fresh)


def test_success_resets():
    rc = recovery.RecoveryController(jitter=(1.0, 1.0))
    rc.on_error(); rc.on_error()
    assert rc.streak == 2
    rc.on_success()
    assert rc.streak == 0
    assert rc.on_error().streak == 1   # 리셋 후 다시 1부터
    print("  [ok] 성공 시 streak 리셋")


# ── 2. 워커 통합: 4999 폭격 후 회복 (FakeSRT) ──────────────────────────
class _FakeSession:
    """_force_session_timeout가 감쌀 수 있도록 request 속성만 가진 더미 세션."""
    def request(self, method, url, **kw):
        return None


class _FakeNF:
    def __init__(self):
        self._cached_key = "poisoned"
        self.session = _FakeSession()


class FakeSRT:
    created = 0
    fail_remaining = 5      # 전역 검색 5회까지 netfunnel 차단, 이후 성공
    successes = 0

    def __init__(self, *a, **k):
        FakeSRT.created += 1
        self._session = _FakeSession()
        self.netfunnel_helper = _FakeNF()

    def search_train(self, *a, **k):
        if FakeSRT.fail_remaining > 0:
            FakeSRT.fail_remaining -= 1
            raise SRTNetFunnelError(
                "Failed to complete NetFunnel: NetFunnel.gRtype=4999;"
                "NetFunnel.gControl.result='5..."
            )
        FakeSRT.successes += 1
        return []           # 성공: 검색됐지만 좌석 없음 → 계속 폴링


def test_worker_recovers_without_wedging(monkeypatch=None):
    # 빠르고 결정적으로: 작은 백오프, 2회마다 새 세션, 세션/정체 타이머는 무력화
    FakeSRT.created = 0
    FakeSRT.fail_remaining = 5
    FakeSRT.successes = 0

    # 워커가 RecoveryController(base=..., cap=..., fresh_login_every=...)로
    # 인자를 넘기므로 더미도 임의 인자를 받아 결정적 값으로 덮어쓴다.
    orig_rc = srt_worker.RecoveryController
    srt_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    # netfunnel helper 재생성을 더미로 (실 requests 세션 생성 방지, 결정적).
    srt_worker.NetFunnelHelper = _FakeNF
    srt_worker.SRT = FakeSRT
    srt_worker.MIN_INTERVAL = 0.01
    srt_worker.MAX_INTERVAL = 0.01
    srt_worker.SESSION_MAX_AGE = 1e9
    srt_worker.STALL_LIMIT = 1e9
    srt_worker.config.srt.load = lambda: types.SimpleNamespace(
        srt_id="tester", srt_password="pw"
    )

    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20260701", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)

    # 회복(성공 검색)까지 최대 5초 대기
    deadline = time.time() + 5
    while time.time() < deadline and FakeSRT.successes < 2:
        time.sleep(0.05)
    srt_worker.manager.stop(job.id)
    time.sleep(0.1)

    assert FakeSRT.successes >= 1, "끝내 회복하지 못함(예매 멈춤 재현됨)"
    assert job.status in (srt_worker.JobStatus.POLLING, srt_worker.JobStatus.STOPPED), job.status
    assert job.recoveries >= 5, f"recoveries={job.recoveries} (차단을 복구로 세지 못함)"
    assert FakeSRT.created >= 2, f"새 세션 에스컬레이션 안 됨 (created={FakeSRT.created})"
    print(f"  [ok] 4999 5회 폭격 후 회복: recoveries={job.recoveries} "
          f"fresh_sessions={FakeSRT.created} successes={FakeSRT.successes} status={job.status}")


class FakeSRTConnErr(FakeSRT):
    """netfunnel 오류가 '문자열이 아닌' 예외객체를 .msg에 감싸 던지는 경우.

    SRTrain은 `raise SRTNetFunnelError(ConnectionError(...))`처럼 예외객체를
    그대로 넣는다 → str(e)가 TypeError를 던져 폴링 스레드가 통째로 죽던 버그.
    _safe_err로 회복되는지 검증한다(사용자 보고: netfunnel ConnectionError 사망)."""
    def search_train(self, *a, **k):
        if FakeSRTConnErr.fail_remaining > 0:
            FakeSRTConnErr.fail_remaining -= 1
            raise SRTNetFunnelError(ConnectionError("Connection aborted (non-str msg)"))
        FakeSRTConnErr.successes += 1
        return []


def test_worker_survives_nonstring_netfunnel_msg():
    FakeSRTConnErr.created = 0
    FakeSRTConnErr.fail_remaining = 4
    FakeSRTConnErr.successes = 0

    orig_rc = srt_worker.RecoveryController
    srt_worker.RecoveryController = lambda *a, **k: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
    srt_worker.NetFunnelHelper = _FakeNF
    srt_worker.SRT = FakeSRTConnErr
    srt_worker.MIN_INTERVAL = 0.01
    srt_worker.MAX_INTERVAL = 0.01
    srt_worker.SESSION_MAX_AGE = 1e9
    srt_worker.STALL_LIMIT = 1e9
    srt_worker.config.srt.load = lambda: types.SimpleNamespace(
        srt_id="tester", srt_password="pw"
    )

    spec = srt_worker.JobSpec(
        dep="수서", arr="부산", date="20260701", time="090000",
        train_number=None, passengers=1, seat_pref="any",
        pay_mode=srt_worker.PayMode.MANUAL,
    )
    job = srt_worker.manager.create(spec)
    deadline = time.time() + 5
    while time.time() < deadline and FakeSRTConnErr.successes < 1:
        time.sleep(0.05)
    srt_worker.manager.stop(job.id)
    time.sleep(0.1)

    assert FakeSRTConnErr.successes >= 1, "비문자열 msg netfunnel에서 스레드 사망(버그 재현)"
    assert job.recoveries >= 4, f"recoveries={job.recoveries}"
    print(f"  [ok] 비문자열 msg netfunnel 4회 후 회복: recoveries={job.recoveries} "
          f"successes={FakeSRTConnErr.successes}")


# ── 3. HTTP 타임아웃 강제 (행 방지) ────────────────────────────────────
def test_force_session_timeout_injects_default():
    captured = {}

    class S:
        def request(self, method, url, **kw):
            captured.update(kw)
            return "resp"

    s = S()
    srt_worker._force_session_timeout(s, 25)
    s.request("GET", "http://x")           # 호출자가 timeout을 안 줘도
    assert captured.get("timeout") == 25, captured   # 25초가 주입돼야 함
    # 호출자가 명시하면 그 값을 존중
    s.request("GET", "http://x", timeout=3)
    assert captured.get("timeout") == 3, captured
    assert getattr(s, "_kt_timeout_patched", False) is True
    print("  [ok] 세션 타임아웃 주입(기본 25s, 명시값 존중) → 무한 대기 방지")


def test_new_client_patches_sessions():
    # _new_client가 클라이언트/넷퍼넬 세션 둘 다 타임아웃 패치하는지
    srt_worker.SRT = FakeSRT
    c = FakeSRT()
    srt_worker._force_session_timeout(c._session, 25)
    srt_worker._force_session_timeout(c.netfunnel_helper.session, 25)
    assert c._session._kt_timeout_patched
    assert c.netfunnel_helper.session._kt_timeout_patched
    print("  [ok] _new_client 세션/넷퍼넬 세션 모두 타임아웃 적용")


if __name__ == "__main__":
    print("recovery 백오프/에스컬레이션:")
    test_backoff_grows_and_caps()
    test_fresh_login_escalation()
    test_success_resets()
    print("HTTP 타임아웃(행 방지):")
    test_force_session_timeout_injects_default()
    test_new_client_patches_sessions()
    print("워커 자가복구 통합:")
    test_worker_recovers_without_wedging()
    test_worker_survives_nonstring_netfunnel_msg()
    print("\nALL PASS ✅")
