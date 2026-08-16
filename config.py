"""
config.py — Configuración compartida de sucursal.
Todos los scripts leen este archivo para saber qué sucursal usar.
"""
import json
from app_paths import DATA_DIR

HERE = DATA_DIR
CONFIG_FILE = HERE / "config_sucursal.json"

# ── Definición de sucursales disponibles ──────────────────
# Agrega o edita sucursales aquí si cambian los nombres en los sistemas.
SUCURSALES = {
    "LIBERTADOR": {
        "nombre":               "Las Compañías — Libertador 1476",
        "tivendo_lista_num":    5,                          # número de fila en tabla Tivendo POS
        "tivendo_lista_erp":    "LISTA LOS LIBERTADORES 1476",  # texto en dropdown ERP
        "mh_local":             "LIBERTADOR 1476",          # opción en Mercadohouse
        "tivendo_bodega":       "LIBERTADOR 1476",          # bodega en Informes de Inventario / Nuevo movimiento
        "tivendo_zona_stock":   "LAS COMPAÑIAS",            # idBodega que devuelve la API de stock (no es literal el nombre de la bodega)
    },
    "CANTERA": {
        "nombre":               "La Cantera 3055",
        "tivendo_lista_num":    None,                       # se detecta por nombre automáticamente
        "tivendo_lista_erp":    "LISTA LA CANTERA 3055",
        "mh_local":             "CANTERA 3055",
        "tivendo_bodega":       "LA CANTERA 3055",
        "tivendo_zona_stock":   "BODEGACENTRAL",            # confirmado: subtitulo de "LA CANTERA 3055" en el buscador de Bodegas
    },
    "BALMACEDA": {
        "nombre":               "Balmaceda 599",
        "tivendo_lista_num":    None,
        "tivendo_lista_erp":    "LISTA BALMACEDA 599",
        "mh_local":             "BALMACEDA 599",
        "tivendo_bodega":       "BALMACEDA 599",
        "tivendo_zona_stock":   "balmaceda599",             # confirmado: subtitulo de "BALMACEDA 599" en el buscador de Bodegas (tal cual, en minusculas)
    },
}

DEFAULT_SUCURSAL = "LIBERTADOR"


def cargar_sucursal() -> dict:
    """Lee la sucursal guardada. Si no hay config, usa la default."""
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            key = data.get("sucursal", DEFAULT_SUCURSAL)
            if key in SUCURSALES:
                return {"key": key, **SUCURSALES[key]}
        except Exception:
            pass
    return {"key": DEFAULT_SUCURSAL, **SUCURSALES[DEFAULT_SUCURSAL]}


def guardar_sucursal(key: str):
    """Guarda la sucursal seleccionada en el archivo de config."""
    CONFIG_FILE.write_text(
        json.dumps({"sucursal": key}, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


def sucursal_activa() -> dict:
    return cargar_sucursal()
