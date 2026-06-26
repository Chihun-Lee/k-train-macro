# K-Rail 매크로 (K-Rail Macro)

**SRT + KTX 통합** 매크로. 한 화면에 두 탭, 동시 실행 가능.

> ⚠ **개인용 한정.** 본인 SRT/코레일 계정·본인 카드로만 사용하세요. 자격증명·카드정보는 **macOS Keychain**에 암호화 저장됩니다. 서버는 `127.0.0.1:8912`에만 바인딩됩니다.

---

## 친구한테 보낼 1줄 가이드 (설치)

친구가 본인 Mac에서 **터미널을 열어** 아래 한 줄 붙여넣고 엔터:

```bash
curl -fsSL https://raw.githubusercontent.com/Chihun-Lee/k-rail-macro/main/install.sh | bash
```

> 또는 [`K-Rail_매크로_설치.command`](https://github.com/Chihun-Lee/k-rail-macro/raw/main/K-Rail_매크로_설치.command) 다운로드 → Finder에서 **우클릭 → 열기**

설치 끝나면 **Launchpad → "K-Rail 매크로"** 검색 → 더블클릭. 종료는 **"K-Rail 매크로 종료"**.

---

## 기능

- **하나의 웹앱에 SRT 탭 + KTX 탭** — 둘이 완전히 독립, 동시 실행 가능
- 폴링 간격: **1~30초 균등 랜덤**
- 결제 모드: 수동 (사용자 확인) / 자동 (즉시 결제)
- anti-bot 자동 회복:
  - SRT NetFunnel "Wrong Server ID" → 캐시 무효화 + 클라이언트 재생성
  - KTX MACRO ERROR → 클라이언트 재생성 (Dynapath 우회 토큰 자동 갱신)
- KTX는 KTX/ITX-새마을/무궁화호/누리로/ITX-청춘 모두 지원
- 토스트 알림 + 실시간 로그
- 자격증명/잡 모두 SRT·KTX 별도 관리 (Keychain 항목 분리)

### 카드 테스트
- **KTX**: 활성. 서울→광명 25일 뒤 평일 첫차 reserve→pay→refund. 4겹 안전장치 (snapshot · PNR 일치 · route/date 검증 · post-audit). 위약금 약 400원/회.
- **SRT**: 비활성. SRT 서버의 reserve_info endpoint가 referer를 무시하고 다른 결제완료 표 정보를 반환하는 설계라, 자동 환불을 안전하게 못 함. SRT 카드 결제 검증은 SRT 앱에서 수동으로.

## 기존 SRT/KTX 단독 사용자

- Keychain 항목 이름이 같음 (`srt-macro` / `ktx-macro`) → **저장한 자격증명 그대로 마이그레이션됨**
- 단독 매크로(8910 / 8911)와 통합 매크로(8912)는 다른 포트라 동시에 실행해도 충돌 없음
- 단독 매크로 안 쓸 거면 `~/Applications/SRT 매크로.app` / `KTX 매크로.app` 삭제 + `kill $(lsof -ti tcp:8910 -sTCP:LISTEN)` 등으로 정리

---

## 직접 빌드 / 개발

```bash
git clone https://github.com/Chihun-Lee/k-rail-macro.git
cd k-rail-macro
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python server.py
# → http://127.0.0.1:8912
```

### 파일 구조

| 파일 | 용도 |
|------|------|
| `server.py` | FastAPI 엔트리, `/api/srt/*` + `/api/ktx/*` 라우팅 |
| `srt_worker.py` | SRT polling/reserve/pay (SRTrain) |
| `ktx_worker.py` | KTX polling/reserve/pay (srtgo) |
| `ktx_korail.py` | srtgo Korail + Dynapath bypass |
| `config.py` | 두 namespace (`config.srt`, `config.ktx`) Keychain 저장 |
| `static/index.html` | 탭 UI, 두 서비스 공통 JS |
| `install.sh` | 친구용 원클릭 설치 |

### 라이선스 / 출처

- [SRTrain](https://github.com/ryanking13/SRT) (MIT) — SRT 클라이언트
- [srtgo](https://github.com/lapis42/srtgo) (MIT) — KTX `pay_with_card` 구현
- Dynapath bypass — [nomadamas/k-skill](https://github.com/nomadamas/k-skill) (MIT)
