"""Ajusta la consola antes de cargar los módulos pesados del ejecutable."""

import os
import sys


def preparar_consola():
    if os.name != "nt":
        return False
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = (ctypes.c_int,)
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        kernel32.GetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint))
        kernel32.GetConsoleMode.restype = ctypes.c_int
        kernel32.SetConsoleMode.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        kernel32.SetConsoleMode.restype = ctypes.c_int
        entrada = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        modo = ctypes.c_uint()
        if not entrada or not kernel32.GetConsoleMode(entrada, ctypes.byref(modo)):
            return False
        # EXTENDED_FLAGS habilita el cambio; QUICK_EDIT pausa el proceso al seleccionar.
        nuevo = (modo.value | 0x0080) & ~0x0040
        return bool(kernel32.SetConsoleMode(entrada, nuevo))
    except (AttributeError, OSError, ValueError):
        return False


if getattr(sys, "frozen", False):
    preparar_consola()
    if not any(opcion in sys.argv for opcion in ("--bot", "--tray", "--auto")):
        print("  Iniciando MHSync...", flush=True)
