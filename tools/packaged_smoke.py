"""Run inside PyInstaller to verify DLLs and UI imports, without game actions.

Set MOCRO_STARTUP_CHECK=1 and build the normal spec with a separate distpath.
"""
import ctypes
import json
import sys

from PySide6.QtCore import qVersion
from PySide6.QtWidgets import QApplication, QWidget
import macro
import desktop


def loaded_library(name):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    kernel.GetModuleHandleW.restype = ctypes.c_void_p
    kernel.GetModuleFileNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint]
    output = ctypes.create_unicode_buffer(32768)
    handle = kernel.GetModuleHandleW(name)
    if not handle or not kernel.GetModuleFileNameW(handle, output, len(output)):
        raise RuntimeError(f'library was not loaded: {name}')
    return output.value


app = QApplication([])
widget = QWidget()
widget.setWindowTitle('Mocro packaged startup check')
app.processEvents()
widget.close()
assert hasattr(macro.Macro, 'settled_character')
assert hasattr(desktop, 'Window')
print(json.dumps({'ok': True, 'frozen': bool(getattr(sys, 'frozen', False)),
                  'qt': qVersion(), 'qt_core': loaded_library('Qt6Core.dll'),
                  'icu': loaded_library('icuuc.dll')}, ensure_ascii=False))
