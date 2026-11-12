"""
Credenciales centralizadas.

En el exe se cargan desde datos de usuario de Windows, no desde un archivo
suelto junto al programa.
"""

import json
from app_paths import DATA_DIR


CRED_FILE = DATA_DIR / "credenciales.json"

DEFAULTS = {
    "TIVENDO_EMAIL": "",
    "TIVENDO_PASSWORD": "",
    "MH_EMAIL": "",
    "MH_PASSWORD": "",
}


def cargar_credenciales():
    data = DEFAULTS.copy()
    if CRED_FILE.exists():
        try:
            saved = json.loads(CRED_FILE.read_text(encoding="utf-8"))
            for key in data:
                if saved.get(key):
                    data[key] = saved[key]
        except Exception:
            pass
    return data


def guardar_credenciales(tv_email, tv_pass, mh_email, mh_pass):
    CRED_FILE.write_text(
        json.dumps(
            {
                "TIVENDO_EMAIL": tv_email,
                "TIVENDO_PASSWORD": tv_pass,
                "MH_EMAIL": mh_email,
                "MH_PASSWORD": mh_pass,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    recargar()


def recargar():
    global TIVENDO_EMAIL, TIVENDO_PASSWORD, MH_EMAIL, MH_PASSWORD
    data = cargar_credenciales()
    TIVENDO_EMAIL = data["TIVENDO_EMAIL"]
    TIVENDO_PASSWORD = data["TIVENDO_PASSWORD"]
    MH_EMAIL = data["MH_EMAIL"]
    MH_PASSWORD = data["MH_PASSWORD"]


recargar()
