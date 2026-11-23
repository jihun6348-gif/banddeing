@echo off
set "PATH=%SystemRoot%\System32;%SystemRoot%;%SystemRoot%\System32\Wbem;%SystemRoot%\System32\WindowsPowerShell\v1.0;%PATH%"
"%SystemRoot%\System32\chcp.com" 65001 >nul
cd /d "%~dp0"
set "UV=%USERPROFILE%\.local\bin\uv.exe"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"

if not exist "%UV%" (
  echo [1/2] 파이썬 실행 도구 uv를 설치하는 중이에요. 1분 정도 걸려요...
  "%PS%" -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
if not exist "%UV%" (
  echo.
  echo uv 설치에 실패했어요. 이 창의 메시지를 캡처해서 보내 주세요.
  pause
  exit /b 1
)

rem 로컬 시연용: 데모 데이터가 든 demo.db를 따로 쓴다 (실제 서비스 데이터와 섞이지 않게)
set "DEMO_MODE=1"
set "DATABASE_PATH=%~dp0demo.db"

echo.
echo [2/2] 반띵 서버를 시작해요. 처음에는 필요한 프로그램을 내려받느라 1~2분 걸려요.
echo "Running on http://127.0.0.1:5000" 이 나오면 브라우저에서 그 주소를 여세요.
echo 서버를 끄려면 이 창을 닫으세요.
echo.
start "" "%ComSpec%" /c "%SystemRoot%\System32\timeout.exe /t 10 >nul & start http://127.0.0.1:5000"
"%UV%" run app.py
pause
