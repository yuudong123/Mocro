@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0dungeon"

rem 처음 실행이면 Python 설치, dungeon\.venv 가상환경 생성, 패키지 설치까지 한다.
set "VENV_PY=%CD%\.venv\Scripts\python.exe"
if exist "%VENV_PY%" goto deps

call :find_python
if not defined BASE_PY call :install_python
if not defined BASE_PY goto fail
echo [준비] 가상환경 만드는 중: dungeon\.venv
"%BASE_PY%" -m venv .venv
if errorlevel 1 goto fail

:deps
rem requirements.txt가 마지막 설치 때와 같으면 건너뛴다.
fc /b requirements.txt .venv\requirements.installed >nul 2>&1 && goto run
echo [준비] 패키지 설치 중...
"%VENV_PY%" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
copy /y requirements.txt .venv\requirements.installed >nul

:run
"%VENV_PY%" macro.py %*
if errorlevel 1 pause
exit /b

:find_python
rem numpy 1.26 휠이 있는 Python 3.11을 쓴다. Microsoft Store 바로가기 python.exe는 피한다.
for /f "delims=" %%i in ('py -3.11 -c "import sys; print(sys.executable)" 2^>nul') do set "BASE_PY=%%i"
if defined BASE_PY exit /b 0
for %%p in ("%LOCALAPPDATA%\Programs\Python\Python311\python.exe" "%ProgramFiles%\Python311\python.exe") do (
    if exist %%p set "BASE_PY=%%~p"
)
exit /b 0

:install_python
where winget >nul 2>&1
if errorlevel 1 (
    echo [오류] Python 3.11이 없고 winget도 없습니다. python.org에서 Python 3.11을 설치한 뒤 다시 실행하세요.
    exit /b 1
)
echo [준비] Python 3.11 설치 중 - winget
winget install -e --id Python.Python.3.11 --scope user --silent --accept-package-agreements --accept-source-agreements
call :find_python
exit /b 0

:fail
echo [오류] 실행 준비에 실패했습니다. 위 메시지를 확인하세요.
pause
exit /b 1
