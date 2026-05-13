import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import credenciales


class CredencialesTests(unittest.TestCase):
    def test_cargar_credenciales_uses_defaults_when_file_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(credenciales, "CRED_FILE", Path(tmp) / "credenciales.json"):
                data = credenciales.cargar_credenciales()

        self.assertEqual(data, credenciales.DEFAULTS)

    def test_cargar_credenciales_merges_saved_values_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credenciales.json"
            path.write_text('{"TIVENDO_EMAIL": "nuevo@example.com"}', encoding="utf-8")
            with patch.object(credenciales, "CRED_FILE", path):
                data = credenciales.cargar_credenciales()

        self.assertEqual(data["TIVENDO_EMAIL"], "nuevo@example.com")
        self.assertEqual(data["TIVENDO_PASSWORD"], credenciales.DEFAULTS["TIVENDO_PASSWORD"])

    def test_guardar_credenciales_writes_and_recarga_globals(self):
        originales = (
            credenciales.TIVENDO_EMAIL,
            credenciales.TIVENDO_PASSWORD,
            credenciales.MH_EMAIL,
            credenciales.MH_PASSWORD,
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with patch.object(credenciales, "CRED_FILE", Path(tmp) / "credenciales.json"):
                    credenciales.guardar_credenciales(
                        "tv@example.com",
                        "tv-pass",
                        "mh@example.com",
                        "mh-pass",
                    )

            self.assertEqual(credenciales.TIVENDO_EMAIL, "tv@example.com")
            self.assertEqual(credenciales.TIVENDO_PASSWORD, "tv-pass")
            self.assertEqual(credenciales.MH_EMAIL, "mh@example.com")
            self.assertEqual(credenciales.MH_PASSWORD, "mh-pass")
        finally:
            (
                credenciales.TIVENDO_EMAIL,
                credenciales.TIVENDO_PASSWORD,
                credenciales.MH_EMAIL,
                credenciales.MH_PASSWORD,
            ) = originales


if __name__ == "__main__":
    unittest.main()
