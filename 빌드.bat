@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

rem 모비 통합 매크로.exe 만들기. 결과: dist\모비 통합 매크로\ (폴더째 배포)
rem 빌드용 가상환경 build\.venv에 공방·던전 패키지와 PyInstaller를 설치해 쓴다.
set "BUILD_PY=%CD%\build\.venv\Scripts\python.exe"
if exist "%BUILD_PY%" goto deps

set "BASE_PY="
for /f "delims=" %%i in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%i"
if not defined BASE_PY for /f "delims=" %%i in ('py -3.12 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%i"
if not defined BASE_PY if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "BASE_PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined BASE_PY (
    echo [오류] Python 3.11 또는 3.12가 필요합니다.
    goto fail
)
echo [준비] 빌드용 가상환경 만드는 중: build\.venv
"%BASE_PY%" -m venv build\.venv
if errorlevel 1 goto fail

:deps
echo [준비] 패키지 설치 중...
"%BUILD_PY%" -m pip install --disable-pip-version-check -q -r workshop\requirements.txt -r dungeon\requirements.txt pyinstaller==6.22.3
if errorlevel 1 goto fail

echo [빌드] 모비 통합 매크로.exe 만드는 중...
"%BUILD_PY%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build\work "모비 통합 매크로.spec"
if errorlevel 1 goto fail
echo.
echo [완료] dist\모비 통합 매크로\모비 통합 매크로.exe
echo        폴더째 옮겨서 쓰세요. 설정·로그는 exe 옆 data 폴더에 저장됩니다.
pause
exit /b 0

:fail
echo [오류] 빌드에 실패했습니다. 위 메시지를 확인하세요.
pause
exit /b 1
