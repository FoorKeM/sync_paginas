import os
import sys
import atexit
import tempfile
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
AJUSTE_DIR = DATA_DIR / "ajustes_stock"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
DIAG_DIR.mkdir(parents=True, exist_ok=True)
DESCARGA_DIR.mkdir(parents=True, exist_ok=True)
AJUSTE_DIR.mkdir(parents=True, exist_ok=True)


def configure_playwright_browsers() -> None:
    """Usa navegador portable si existe; si no, se usara Edge/Chrome del sistema."""
    portable_browsers = APP_DIR / "ms-playwright"
    if portable_browsers.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(portable_browsers))


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
