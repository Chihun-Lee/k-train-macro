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
class _FakeNF:
    def __init__(self):
        self._cached_key = "poisoned"


class FakeSRT:
    created = 0
    fail_remaining = 5      # 전역 검색 5회까지 netfunnel 차단, 이후 성공
    successes = 0

    def __init__(self, *a, **k):
        FakeSRT.created += 1
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

    orig_rc = srt_worker.RecoveryController
    srt_worker.RecoveryController = lambda: orig_rc(
        base=0.01, cap=0.05, fresh_login_every=2, jitter=(1.0, 1.0)
    )
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


if __name__ == "__main__":
    print("recovery 백오프/에스컬레이션:")
    test_backoff_grows_and_caps()
    test_fresh_login_escalation()
    test_success_resets()
    print("워커 자가복구 통합:")
    test_worker_recovers_without_wedging()
    print("\nALL PASS ✅")
