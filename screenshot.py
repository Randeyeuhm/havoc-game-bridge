#!/usr/bin/env python3
"""
Roblox window capturer for the game bridge (stdlib only, no pip installs).

Scans every top-level window whose process is Roblox (player or studio),
captures one to PNG, and returns the saved path - so an MCP client (or a
human) can *see* the game: ESP drawings, menu state, visuals.

Two capture paths, tried in order:
  1. PrintWindow(PW_RENDERFULLCONTENT) - works while the window is occluded
     on many setups;
  2. screen BitBlt of the window rect - what is actually visible (fallback
     when PrintWindow comes back black, which some D3D windows do).
Focus is never stolen by default (`focus=True` restores + raises first).

CLI:
    python screenshot.py --list                # JSON window list
    python screenshot.py                       # capture the largest window
    python screenshot.py --index 2 --out x.png # pick one / choose the file
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import sys
import time
import zlib
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000
BI_RGB = 0
DIB_RGB_COLORS = 0
SW_RESTORE = 9

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# HRESULT-free, best-effort per-monitor DPI awareness so coordinates are
# physical pixels on scaled displays
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD), ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


# --- prototypes: handle-returning functions MUST be bound or their 64-bit
# --- return values truncate to 32-bit ints and every call silently fails
user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
user32.EnumWindows.restype = wintypes.BOOL
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.PrintWindow.argtypes = (wintypes.HWND, wintypes.HDC, wintypes.UINT)
user32.PrintWindow.restype = wintypes.BOOL
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = (wintypes.HWND,)
user32.ReleaseDC.argtypes = (wintypes.HWND, wintypes.HDC)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = (wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD)
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = (wintypes.HDC, ctypes.c_void_p)
gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
gdi32.DeleteDC.argtypes = (wintypes.HDC,)
gdi32.BitBlt.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
gdi32.BitBlt.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD,
                                                wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)


def _process_exe(pid: int) -> str:
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        kernel32.CloseHandle(h)


def _window_info(hwnd: int) -> dict | None:
    try:
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _process_exe(pid.value)
        base = os.path.basename(exe).lower()
        if "roblox" not in base:
            return None
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        w, h = rect.right - rect.left, rect.bottom - rect.top
        if w < 100 or h < 100 or not user32.IsWindowVisible(hwnd):
            return None
        title = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title, 512)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        kind = "studio" if "studio" in base else ("player" if "player" in base else "other")
        return {
            "hwnd": int(hwnd),
            "pid": int(pid.value),
            "exe": os.path.basename(exe),
            "kind": kind,
            "title": title.value,
            "class": cls.value,
            "rect": [rect.left, rect.top, w, h],
            "area": w * h,
            "foreground": bool(user32.GetForegroundWindow() == hwnd),
        }
    except Exception:
        return None


def list_roblox_windows(kinds=("player", "studio", "other")) -> list[dict]:
    """Every visible Roblox top-level window, largest first."""
    found: list[dict] = []

    @WNDENUMPROC
    def cb(hwnd, _lparam):
        try:
            info = _window_info(hwnd)
            if info and info["kind"] in kinds:
                found.append(info)
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    found.sort(key=lambda i: (-i["area"], i["pid"]))
    for i, info in enumerate(found, 1):
        info["index"] = i
    return found


def _grab_bits(hwnd: int, rect: list[int]) -> tuple[bytes, int, int, str]:
    """Capture the window; returns (bgra_bytes, w, h, method)."""
    left, top, w, h = rect
    hdc_screen = user32.GetDC(0)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down rows
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB
    bits_ptr = ctypes.c_void_p()
    hbmp = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS,
                                  ctypes.byref(bits_ptr), None, 0)
    old = gdi32.SelectObject(hdc_mem, hbmp)
    method = "printwindow"
    ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)
    bits = ctypes.string_at(bits_ptr, w * h * 4) if bits_ptr else b""

    def blackish(data: bytes) -> bool:
        if not data:
            return True
        total, n = 0, 0
        for i in range(0, len(data) - 4, 4 * 137):
            total += data[i] + data[i + 1] + data[i + 2]
            n += 3
        return (total / max(n, 1)) < 4.0

    if not ok or blackish(bits):
        gdi32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, left, top, SRCCOPY | CAPTUREBLT)
        bits = ctypes.string_at(bits_ptr, w * h * 4) if bits_ptr else b""
        method = "screen-bitblt"

    gdi32.SelectObject(hdc_mem, old)
    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return bits, w, h, method


def _write_png(path: str, bgra: bytes, w: int, h: int) -> None:
    rows = []
    stride = w * 4
    for y in range(h):
        row = bytearray(bgra[y * stride:(y + 1) * stride])
        # BGRA -> RGBA
        row[0::4], row[2::4] = row[2::4], row[0::4]
        rows.append(b"\x00" + bytes(row))
    raw = b"".join(rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(png)


def capture(hwnd: int, out_path: str, focus: bool = False) -> dict:
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise RuntimeError("GetWindowRect failed for hwnd %d" % hwnd)
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w < 4 or h < 4:
        raise RuntimeError("window rect is empty (%dx%d)" % (w, h))
    if focus:
        try:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
        except Exception:
            pass
    bits, w, h, method = _grab_bits(hwnd, [rect.left, rect.top, w, h])
    if not bits:
        raise RuntimeError("capture returned no pixels")
    _write_png(out_path, bits, w, h)
    return {"path": os.path.abspath(out_path), "width": w, "height": h,
            "method": method, "bytes": os.path.getsize(out_path)}


def capture_by_args(index: int = 0, pid: int = 0, hwnd: int = 0,
                    out: str = "", focus: bool = False) -> dict:
    if hwnd:
        target = {"hwnd": hwnd, "index": -1}
    else:
        wins = list_roblox_windows()
        if pid:
            wins = [w for w in wins if w["pid"] == pid]
        if not wins:
            raise RuntimeError("no Roblox window found (is the game running?)")
        target = wins[max(0, min(index - 1, len(wins) - 1))] if index else wins[0]
    if not out:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "shots", "roblox_%s.png" % stamp)
    result = capture(target["hwnd"], out, focus=focus)
    result["hwnd"] = target["hwnd"]
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Roblox window capturer (stdlib only)")
    ap.add_argument("--list", action="store_true", help="print the window list as JSON")
    ap.add_argument("--index", type=int, default=0, help="1-based window index from --list")
    ap.add_argument("--pid", type=int, default=0, help="pick the window by process id")
    ap.add_argument("--hwnd", type=int, default=0, help="pick the window by hwnd")
    ap.add_argument("--out", default="", help="output PNG path (default shots/roblox_<ts>.png)")
    ap.add_argument("--focus", action="store_true", help="restore + raise the window before capture")
    args = ap.parse_args()

    if args.list:
        print(json.dumps(list_roblox_windows(), indent=2))
        return 0
    try:
        result = capture_by_args(args.index, args.pid, args.hwnd, args.out, args.focus)
    except RuntimeError as exc:
        print("error: %s" % exc)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
