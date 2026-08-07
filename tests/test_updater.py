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


class DescargarActualizacionTests(unittest.TestCase):
    def test_writes_file_and_reports_cumulative_progress(self):
        import tempfile
        from pathlib import Path

        resp = MagicMock()
        resp.headers = {"Content-Length": "15"}
        resp.read.side_effect = [b"a" * 10, b"b" * 5, b""]
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False

        progreso = []
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "nuevo.exe"
            with patch("updater.urllib.request.urlopen", return_value=cm):
                updater.descargar_actualizacion(
                    "http://x", destino, progreso_fn=lambda d, t: progreso.append((d, t))
                )
            self.assertEqual(destino.read_bytes(), b"a" * 10 + b"b" * 5)

        self.assertEqual(progreso, [(10, 15), (15, 15)])

    def test_progreso_fn_is_optional(self):
        import tempfile
        from pathlib import Path

        resp = MagicMock()
        resp.headers = {}
        resp.read.side_effect = [b"contenido", b""]
        cm = MagicMock()
        cm.__enter__.return_value = resp
        cm.__exit__.return_value = False

        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp) / "nuevo.exe"
            with patch("updater.urllib.request.urlopen", return_value=cm):
                updater.descargar_actualizacion("http://x", destino)
            self.assertEqual(destino.read_bytes(), b"contenido")


class AplicarActualizacionTests(unittest.TestCase):
    def test_raises_when_not_frozen(self):
        with patch("updater.sys.frozen", create=True, new=False):
            with self.assertRaises(Exception):
                updater.aplicar_actualizacion_y_reiniciar(None, "MHSync_V1.9.1.exe")

    def test_bat_script_renames_to_new_version_filename(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp).resolve()
            exe_viejo = tmp_dir / "MHSync_V1.9.0.exe"
            exe_viejo.write_text("viejo", encoding="utf-8")
            nuevo_exe = tmp_dir / "descarga_temp.exe"
            nuevo_exe.write_text("nuevo", encoding="utf-8")

            with (
                patch("updater.sys.frozen", create=True, new=True),
                patch("updater.sys.executable", str(exe_viejo)),
                patch("updater.subprocess.Popen") as popen,
                patch("updater.sys.exit") as exit_mock,
            ):
                updater.aplicar_actualizacion_y_reiniciar(nuevo_exe, "MHSync_V1.9.1.exe")

            bat_path = tmp_dir / "_actualizar_mhsync.bat"
            contenido = bat_path.read_text(encoding="utf-8")

            self.assertIn(str(tmp_dir / "MHSync_V1.9.1.exe"), contenido)
            self.assertIn(f'del /F /Q "{exe_viejo}"', contenido)
            popen.assert_called_once()
            exit_mock.assert_called_once_with(0)


if __name__ == "__main__":
    unittest.main()
