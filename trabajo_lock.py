"""Evita sincronizaciones simultáneas entre menú, bot y tarea programada."""

import os
from contextlib import contextmanager

from app_paths import USER_DATA_DIR


@contextmanager
def _bloqueo(nombre):
    ruta = USER_DATA_DIR / nombre
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.touch(exist_ok=True)
    with ruta.open("r+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def trabajo_exclusivo():
    return _bloqueo("trabajo.lock")


def bot_exclusivo():
    return _bloqueo("telegram.lock")


def configuracion_telegram_exclusiva():
    return _bloqueo("telegram_config.lock")
