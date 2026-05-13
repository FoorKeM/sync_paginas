import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config


class ConfigTests(unittest.TestCase):
    def test_cargar_sucursal_uses_default_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "CONFIG_FILE", Path(tmp) / "config.json"):
                sucursal = config.cargar_sucursal()

        self.assertEqual(sucursal["key"], config.DEFAULT_SUCURSAL)

    def test_guardar_and_cargar_sucursal_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "CONFIG_FILE", Path(tmp) / "config.json"):
                config.guardar_sucursal("CANTERA")
                sucursal = config.cargar_sucursal()

        self.assertEqual(sucursal["key"], "CANTERA")
        self.assertEqual(sucursal["mh_local"], config.SUCURSALES["CANTERA"]["mh_local"])

    def test_cargar_sucursal_ignores_invalid_saved_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"sucursal": "NO_EXISTE"}', encoding="utf-8")
            with patch.object(config, "CONFIG_FILE", path):
                sucursal = config.cargar_sucursal()

        self.assertEqual(sucursal["key"], config.DEFAULT_SUCURSAL)


if __name__ == "__main__":
    unittest.main()
