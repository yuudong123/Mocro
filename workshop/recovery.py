"""Bounded ESC recovery: only a verified foreground game window receives input."""
import ctypes
from ctypes import wintypes
import os


def send_game_escape():
    if os.name != 'nt':
        return False
    user = ctypes.WinDLL('user32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetForegroundWindow.restype = wintypes.HWND
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    hwnd = user.GetForegroundWindow()
    if not hwnd:
        return False
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    handle = kernel.OpenProcess(0x1000, False, pid.value)
    if not handle:
        return False
    try:
        size = wintypes.DWORD(32768)
        path = ctypes.create_unicode_buffer(size.value)
        if not kernel.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
            return False
        if os.path.basename(path.value).lower() != 'mabinogimobile.exe':
            return False
    finally:
        kernel.CloseHandle(handle)
    if user.GetForegroundWindow() != hwnd:
        return False
    # KEYBDINPUT union must include the larger MOUSEINPUT member for correct ABI.
    class Key(ctypes.Structure):
        _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD), ('flags', wintypes.DWORD),
                    ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
    class Mouse(ctypes.Structure):
        _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG), ('data', wintypes.DWORD),
                    ('flags', wintypes.DWORD), ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
    class Union(ctypes.Union):
        _fields_ = [('key', Key), ('mouse', Mouse)]
    class Input(ctypes.Structure):
        _fields_ = [('type', wintypes.DWORD), ('value', Union)]
    inputs = (Input * 2)()
    inputs[0].type = inputs[1].type = 1
    inputs[0].value.key.vk = inputs[1].value.key.vk = 0x1B
    inputs[1].value.key.flags = 2
    user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
    return user.SendInput(2, inputs, ctypes.sizeof(Input)) == 2
