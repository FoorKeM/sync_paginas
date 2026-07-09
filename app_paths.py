import os
import sys
import atexit
import asyncio
import subprocess
import tempfile
import threading
import time
from pathlib import Path


IS_FROZEN = getattr(sys, "frozen", False)


def app_dir() -> Path:
    """Carpeta editable de la app: origen en Python, carpeta del exe al empaquetar."""
    if IS_FROZEN:
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _local_app_data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / "MercadohouseSync"
    return Path.home() / "AppData" / "Local" / "MercadohouseSync"


APP_DIR = app_dir()
DATA_DIR = _local_app_data_dir() if IS_FROZEN else APP_DIR
RUNTIME_DIR = (Path(tempfile.gettempdir()) / "MercadohouseSync") if IS_FROZEN else APP_DIR
DIAG_DIR = DATA_DIR / "diagnosticos"
DESCARGA_DIR = DATA_DIR / "downloads"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DIAG_DIR.mkdir(parents=True, exist_ok=True)
DESCARGA_DIR.mkdir(parents=True, exist_ok=True)


def configure_playwright_browsers() -> None:
    """Usa navegador portable si existe; si no, se usara Edge/Chrome del sistema."""
    hide_child_console_windows()
    start_child_window_hider()
    portable_browsers = APP_DIR / "ms-playwright"
    if portable_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(portable_browsers))


def hide_child_console_windows() -> None:
    """Evita ventanas negras de procesos hijos como el driver Node de Playwright."""
    if os.name != "nt" or getattr(asyncio, "_mercadohouse_no_console_patch", False):
        return

    original_exec = asyncio.create_subprocess_exec
    original_shell = asyncio.create_subprocess_shell

    async def create_subprocess_exec_no_window(*args, **kwargs):
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        return await original_exec(*args, **kwargs)

    async def create_subprocess_shell_no_window(*args, **kwargs):
        kwargs.setdefault("creationflags", subprocess.CREATE_NO_WINDOW)
        return await original_shell(*args, **kwargs)

    asyncio.create_subprocess_exec = create_subprocess_exec_no_window
    asyncio.create_subprocess_shell = create_subprocess_shell_no_window
    asyncio._mercadohouse_no_console_patch = True


def start_child_window_hider() -> None:
    """Oculta ventanas accidentales abiertas por Edge/Node hijos del sync."""
    if os.name != "nt" or getattr(sys, "_mercadohouse_window_hider", False):
        return
    sys._mercadohouse_window_hider = True

    def worker() -> None:
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            SW_HIDE = 0
            own_pid = os.getpid()
            TH32CS_SNAPPROCESS = 0x00000002
            INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

            EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            user32.EnumWindows.argtypes = [EnumWindowsProc, wintypes.LPARAM]
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
            kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            def child_pids() -> set[int]:
                current = {own_pid}
                pairs = []
                snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
                if snapshot == INVALID_HANDLE_VALUE:
                    return set()
                try:
                    entry = PROCESSENTRY32W()
                    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                    if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                        return set()
                    while True:
                        pairs.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
                        if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                            break
                finally:
                    kernel32.CloseHandle(snapshot)
                changed = True
                while changed:
                    changed = False
                    for pid, parent in pairs:
                        if parent in current and pid not in current:
                            current.add(pid)
                            changed = True
                current.discard(own_pid)
                return current

            def hide_windows_for(pids: set[int]) -> None:
                if not pids:
                    return

                def callback(hwnd, _):
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    pid = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    if int(pid.value) in pids:
                        user32.ShowWindow(hwnd, SW_HIDE)
                    return True

                user32.EnumWindows(EnumWindowsProc(callback), 0)

            end = time.time() + 60 * 30
            while time.time() < end:
                hide_windows_for(child_pids())
                time.sleep(0.5)
        except Exception:
            return

    threading.Thread(target=worker, daemon=True).start()


def configure_console_encoding() -> None:
    """Evita caidas si CMD no puede imprimir caracteres Unicode del menu."""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def runtime_path(name: str) -> Path:
    return RUNTIME_DIR / name


def cleanup_runtime_files() -> None:
    if not IS_FROZEN:
        return
    for pattern in ("error*.png", "error_art*.png"):
        for path in RUNTIME_DIR.glob(pattern):
            try:
                path.unlink()
            except Exception:
                pass
    try:
        RUNTIME_DIR.rmdir()
    except Exception:
        pass


atexit.register(cleanup_runtime_files)
