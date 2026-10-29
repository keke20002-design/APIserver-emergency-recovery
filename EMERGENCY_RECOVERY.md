# APIserver 긴급 복구

GitHub의 이 저장소가 공개용 긴급 복구본이다. 비밀 값은 포함하지 않는다.

## 복구 방법

1. 저장소를 내려받는다.
2. 보관해 둔 `.env`를 프로젝트 최상위 폴더에 넣는다.
3. Python 3.11 이상에서 다음 명령을 실행한다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\start_server.bat
```

서버 확인 주소는 `http://localhost:8000/docs`다.

서비스 계정 JSON은 `.env`의 `FIREBASE_SERVICE_ACCOUNT_JSON`,
`PLAY_SERVICE_ACCOUNT_JSON`, `ANNUAL_LEAVE_FIREBASE_SERVICE_ACCOUNT_JSON`에
한 줄 JSON으로 넣으면 별도 인증 파일 없이 복구할 수 있다.

## 복구본 갱신

APIserver 코드 변경을 마친 뒤 다음을 실행한다.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\update_emergency_backup.ps1
```

로컬 `api_emergency_backup\public`에는 비밀정보가 제거된 전체 코드가,
`api_emergency_backup\private`에는 `.env`와 `.secrets`가 보관된다.
스크립트는 공개용 파일에서 비밀 파일 이름과 일반적인 비밀 값 패턴도 검사한다.
