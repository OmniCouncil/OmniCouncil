"""macOS 全局快捷键（Carbon RegisterEventHotKey，经 ctypes 调用，无第三方依赖）。

为什么不用 `keyboard` 库：它在 macOS 上要求以 root 运行（否则监听线程报
"Error 13 - Must be run as administrator"），并且无法映射字母键（'j' is not mapped）。
RegisterEventHotKey 是系统为「全局快捷键」提供的标准接口：无需 root，也无需「辅助功能」权限，
回调在主线程（Qt 的 Cocoa 事件循环）中触发，可直接操作窗口。

    hk = GlobalHotkey("cmd+shift+j", callback)
    hk.register()   # 返回 (ok, message)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys
from typing import Callable, Optional

from i18n import t

log = logging.getLogger("agentmanager.hotkey")

# Carbon 修饰键位（Events.h）
MODIFIERS = {
    "cmd": 1 << 8, "command": 1 << 8, "⌘": 1 << 8,
    "shift": 1 << 9, "⇧": 1 << 9,
    "option": 1 << 11, "opt": 1 << 11, "alt": 1 << 11, "⌥": 1 << 11,
    "ctrl": 1 << 12, "control": 1 << 12, "⌃": 1 << 12,
}
MODIFIER_SYMBOLS = [(1 << 12, "⌃"), (1 << 11, "⌥"), (1 << 9, "⇧"), (1 << 8, "⌘")]

# ANSI 键盘虚拟键码（HIToolbox/Events.h kVK_*）
KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46,
    ".": 47, "`": 50, "return": 36, "enter": 36, "tab": 48, "space": 49, "escape": 53, "esc": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111,
}

def parse_hotkey(spec: str) -> tuple[int, int]:
    """'cmd+shift+j' → (keycode, carbon_modifiers)。"""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+") if p.strip()]
    if not parts:
        raise ValueError(t("hk.empty"))
    *mods, key = parts
    if key not in KEYCODES:
        raise ValueError(t("hk.bad_key", key=key))
    flags = 0
    for m in mods:
        if m not in MODIFIERS:
            raise ValueError(t("hk.bad_mod", mod=m))
        flags |= MODIFIERS[m]
    if not flags:
        raise ValueError(t("hk.need_mod"))
    return KEYCODES[key], flags


def pretty_hotkey(spec: str) -> str:
    """'cmd+shift+j' → '⌘⇧J'（按 macOS 习惯顺序）。"""
    try:
        _, flags = parse_hotkey(spec)
    except ValueError:
        return spec
    key = spec.replace(" ", "").split("+")[-1]
    symbols = "".join(sym for bit, sym in MODIFIER_SYMBOLS if flags & bit)
    return symbols + ({"space": "Space", "return": "↩", "enter": "↩"}.get(key.lower(), key.upper()))


def _fourcc(s: str) -> int:
    return int.from_bytes(s.encode("ascii"), "big")


class _EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class _EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


_HANDLER_PROC = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
_kEventClassKeyboard = _fourcc("keyb")
_kEventHotKeyPressed = 5


class GlobalHotkey:
    def __init__(self, spec: str, callback: Callable[[], None]):
        self.spec = spec
        self.callback = callback
        self._hotkey_ref = ctypes.c_void_p()
        self._handler_ref = ctypes.c_void_p()
        self._proc: Optional[_HANDLER_PROC] = None  # 必须持有引用，防止回调被 GC
        self.registered = False

    def register(self) -> tuple[bool, str]:
        if sys.platform != "darwin":
            return False, t("hk.mac_only")
        try:
            keycode, mods = parse_hotkey(self.spec)
        except ValueError as e:
            return False, t("hk.invalid", spec=self.spec, e=e)

        carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, _HANDLER_PROC, ctypes.c_ulong,
                                               ctypes.POINTER(_EventTypeSpec), ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, _EventHotKeyID, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        self._carbon = carbon
        target = carbon.GetApplicationEventTarget()

        def handler(_call_ref, _event, _user_data) -> int:
            try:
                self.callback()
            except Exception:  # 回调异常不能抛回 Carbon
                log.exception("快捷键回调出错")
            return 0

        self._proc = _HANDLER_PROC(handler)
        spec = _EventTypeSpec(_kEventClassKeyboard, _kEventHotKeyPressed)
        err = carbon.InstallEventHandler(target, self._proc, 1, ctypes.byref(spec), None,
                                         ctypes.byref(self._handler_ref))
        if err != 0:
            return False, t("hk.handler_failed", err=err, hint=t("hk.hint"))

        err = carbon.RegisterEventHotKey(keycode, mods, _EventHotKeyID(_fourcc("AgMg"), 1), target, 0,
                                         ctypes.byref(self._hotkey_ref))
        if err != 0:
            reason = t("hk.taken") if err == -9878 else f"OSStatus {err}"
            return False, t("hk.failed", key=pretty_hotkey(self.spec), reason=reason, hint=t("hk.hint"))
        self.registered = True
        return True, t("hk.registered", key=pretty_hotkey(self.spec), hint=t("hk.hint"))

    def unregister(self) -> None:
        if self.registered:
            self._carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
            self._carbon.UnregisterEventHotKey(self._hotkey_ref)
            self.registered = False


def activate_app() -> None:
    """把本进程的应用切到前台（Qt 的 activateWindow 在应用处于后台时不会抢占焦点）。"""
    if sys.platform != "darwin":
        return
    objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
    objc.objc_getClass.restype = ctypes.c_void_p
    objc.sel_registerName.restype = ctypes.c_void_p
    send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
    send_bool = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool)(("objc_msgSend", objc))
    ns_app = send_id(objc.objc_getClass(b"NSApplication"), objc.sel_registerName(b"sharedApplication"))
    send_bool(ns_app, objc.sel_registerName(b"activateIgnoringOtherApps:"), True)
