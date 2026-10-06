# PyInstaller 설정: 에린 공방(생활)과 던전 매크로를 관리자 권한 exe 하나로 묶는다. 빌드.bat이 쓴다.
# 결과: dist/모비 통합 매크로/모비 통합 매크로.exe (폴더째 배포). 설정·로그는 exe 옆 data 폴더에 쓴다.
from pathlib import Path

root = Path(SPECPATH)
name = '모비 통합 매크로'

a = Analysis(
    [str(root / 'workshop' / 'desktop.py')],
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
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name=name,
    icon=str(root / 'workshop' / 'assets' / 'workshop.ico'),
    console=False,
    uac_admin=True,  # 게임이 관리자 권한이라 화면 인식·입력에 관리자 권한이 필요하다
)
coll = COLLECT(exe, a.binaries, a.datas, name=name)
