# APIserver 긴급 복구본

이 저장소는 APIserver 장애 시 다른 Windows PC에서 서버를 복구하기 위한 공개용 소스입니다.
API 키와 비밀번호는 포함되어 있지 않으므로 기존 서버에서 보관한 `.env` 파일이 필요합니다.

## 임시 PC에서 구동하기

### 1. 준비

- Windows 10 또는 11
- 인터넷 연결
- Python 3.11 이상

Python 설치 화면에서 **Add Python to PATH**를 선택합니다. 설치 후 PowerShell에서 확인합니다.

```powershell
python --version
```

### 2. 코드 다운로드

Git을 사용한다면:

```powershell
git clone https://github.com/keke20002-design/APIserver-emergency-recovery.git
cd APIserver-emergency-recovery
```

Git이 없다면 GitHub 페이지의 **Code → Download ZIP**으로 내려받고 압축을 풉니다.

### 3. `.env` 넣기

비공개로 보관한 `.env`를 다운로드한 프로젝트의 최상위 폴더에 복사합니다.
다음처럼 `main.py`, `requirements.txt`, `.env`가 같은 폴더에 있어야 합니다.

```text
APIserver-emergency-recovery/
├── .env
├── main.py
├── requirements.txt
└── start_server.bat
```

파일명이 `.env.txt`로 바뀌지 않았는지 확인합니다. `.env`는 GitHub에 업로드하면 안 됩니다.

### 4. 최초 한 번만 설치

프로젝트 폴더에서 PowerShell을 열고 실행합니다.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Playwright를 사용하는 기능도 필요하다면 브라우저를 추가로 설치합니다.

```powershell
.\.venv\Scripts\python.exe -m playwright install chromium
```

### 5. 서버 시작

`start_server.bat`을 더블클릭하거나 PowerShell에서 실행합니다.

```powershell
.\start_server.bat
```

가상환경 `.venv`가 있으면 배치 파일이 자동으로 그 안의 Python을 사용합니다.
정상적으로 시작되면 다음 주소에서 API 목록을 확인할 수 있습니다.

- API 문서: http://localhost:8000/docs
- 서버 주소: http://localhost:8000

같은 네트워크의 다른 기기에서는 Windows 방화벽에서 포트 `8000`을 허용한 뒤
`http://임시PC의-IP주소:8000/docs`로 접속합니다.

서버를 끄려면 실행 창에서 `Ctrl+C`를 누릅니다.

## 자주 발생하는 문제

- `python을 찾을 수 없습니다`: Python을 다시 설치하면서 **Add Python to PATH**를 선택합니다.
- `No module named ...`: 4번의 패키지 설치 명령을 다시 실행합니다.
- `.env` 관련 오류: `.env`가 `main.py` 옆에 있는지, 파일명이 `.env.txt`가 아닌지 확인합니다.
- 포트 사용 오류: 기존 APIserver 창을 종료하거나 포트 `8000`을 사용하는 프로세스를 종료합니다.

백업 생성과 관리 방법은 [EMERGENCY_RECOVERY.md](EMERGENCY_RECOVERY.md)를 참고하세요.
