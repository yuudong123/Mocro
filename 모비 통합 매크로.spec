# PyInstaller 설정: 에린 공방(생활)과 던전 매크로를 관리자 권한 exe 하나로 묶는다. 빌드.bat이 쓴다.
# 결과: dist/모비 통합 매크로/모비 통합 매크로.exe (폴더째 배포). 설정·로그는 exe 옆 data 폴더에 쓴다.
from pathlib import Path
import os
import sys

root = Path(SPECPATH)
smoke = os.environ.get('MOCRO_STARTUP_CHECK') == '1'
name = 'MocroStartupCheck' if smoke else '모비 통합 매크로'

if sys.platform == 'win32':
    # Dependency discovery must not pick up unrelated Poppler/libheif DLLs
    # from the caller's PATH. Qt uses the Windows ICU ABI, not ICU's versioned ABI.
    windows = Path(os.environ['SystemRoot'])
    os.environ['PATH'] = os.pathsep.join([
        str(Path(sys.executable).parent), sys.base_prefix,
        str(windows / 'System32'), str(windows),
    ])

a = Analysis(
    [str(root / 'tools' / 'packaged_smoke.py' if smoke else root / 'workshop' / 'desktop.py')],
    pathex=[str(root / 'workshop'), str(root / 'dungeon')],
    datas=[
        (str(root / 'workshop' / 'assets'), 'workshop/assets'),
        (str(root / 'workshop' / 'recipes.json'), 'workshop'),
        (str(root / 'workshop' / 'facilities.json'), 'workshop'),
        (str(root / 'dungeon' / 'templates'), 'dungeon/templates'),
    ],
    # 던전 탭은 시작할 때 macro를 읽는다(공방을 열 때 OpenCV를 미리 읽지 않으려고).
    hiddenimports=['macro', 'common', 'roster'],
)
if sys.platform == 'win32':
    # These belong to Windows. Bundling a namesake from another program can
    # shadow the OS library even on a machine with all dependencies installed.
    a.binaries = [entry for entry in a.binaries
                  if not Path(entry[0]).name.lower().startswith(('api-ms-win-', 'ext-ms-win-'))
                  and Path(entry[0]).name.lower() not in ('icuuc.dll', 'ucrtbase.dll')]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=name,
    icon=str(root / 'workshop' / 'assets' / 'workshop.ico'),
    console=smoke,
    uac_admin=not smoke,  # 실제 앱만 관리자 권한으로, 포장 점검은 조작 없이 실행
)
coll = COLLECT(exe, a.binaries, a.datas, name=name)
