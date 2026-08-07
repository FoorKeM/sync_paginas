import json
import unittest
from unittest.mock import MagicMock, patch

import updater


def _fake_response(payload: dict):
    body = json.dumps(payload).encode("utf-8")
    cm = MagicMock()
    cm.__enter__.return_value.read.return_value = body
    cm.__exit__.return_value = False
    return cm


class UpdaterVersionTests(unittest.TestCase):
    def test_version_tuple_parses_semver_with_date_suffix(self):
        self.assertEqual(updater._version_tuple("v1.8.2-2026-08-07"), (1, 8, 2))

    def test_version_tuple_parses_plain_semver(self):
        self.assertEqual(updater._version_tuple("v1.9.0"), (1, 9, 0))

    def test_version_tuple_defaults_missing_parts_to_zero(self):
        self.assertEqual(updater._version_tuple("v2"), (2, 0, 0))


class BuscarReleaseNuevoTests(unittest.TestCase):
    def test_returns_none_when_remote_version_is_not_newer(self):
        payload = {"tag_name": "v1.8.2", "assets": [{"name": "MHSync_V1.8.2.exe", "browser_download_url": "u"}]}
        with patch("updater.urllib.request.urlopen", return_value=_fake_response(payload)):
            self.assertIsNone(updater.buscar_release_nuevo("v1.8.2-2026-08-07"))

    def test_returns_none_when_no_exe_asset_present(self):
        payload = {"tag_name": "v1.9.0", "assets": [{"name": "source.zip", "browser_download_url": "u"}]}
        with patch("updater.urllib.request.urlopen", return_value=_fake_response(payload)):
            self.assertIsNone(updater.buscar_release_nuevo("v1.8.2-2026-08-07"))

    def test_returns_none_on_network_error(self):
        with patch("updater.urllib.request.urlopen", side_effect=OSError("sin internet")):
            self.assertIsNone(updater.buscar_release_nuevo("v1.8.2-2026-08-07"))

    def test_returns_release_info_when_newer_version_with_exe_available(self):
        payload = {
            "tag_name": "v1.9.0",
            "body": "Auto-actualizacion",
            "assets": [
                {"name": "source.zip", "browser_download_url": "zip-url"},
                {"name": "MHSync_V1.9.0.exe", "browser_download_url": "exe-url"},
            ],
        }
        with patch("updater.urllib.request.urlopen", return_value=_fake_response(payload)):
            info = updater.buscar_release_nuevo("v1.8.2-2026-08-07")

        self.assertEqual(
            info,
            {
                "version": "v1.9.0",
                "notas": "Auto-actualizacion",
                "url_descarga": "exe-url",
                "nombre_archivo": "MHSync_V1.9.0.exe",
            },
        )


class AplicarActualizacionTests(unittest.TestCase):
    def test_raises_when_not_frozen(self):
        with patch("updater.sys.frozen", create=True, new=False):
            with self.assertRaises(Exception):
                updater.aplicar_actualizacion_y_reiniciar(None)


if __name__ == "__main__":
    unittest.main()
