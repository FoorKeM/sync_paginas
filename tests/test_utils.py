import tempfile
import unittest
from pathlib import Path

from utils import limpiar_carpeta_archivos, reintentar_accion


class UtilsTests(unittest.IsolatedAsyncioTestCase):
    def test_limpiar_carpeta_archivos_deletes_only_direct_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            (carpeta / "uno.xlsx").write_text("1", encoding="utf-8")
            (carpeta / "dos.txt").write_text("2", encoding="utf-8")
            subcarpeta = carpeta / "sub"
            subcarpeta.mkdir()
            (subcarpeta / "queda.xlsx").write_text("3", encoding="utf-8")

            borrados = limpiar_carpeta_archivos(str(carpeta))

            self.assertEqual(borrados, 2)
            self.assertFalse((carpeta / "uno.xlsx").exists())
            self.assertFalse((carpeta / "dos.txt").exists())
            self.assertTrue(subcarpeta.exists())
            self.assertTrue((subcarpeta / "queda.xlsx").exists())

    async def test_reintentar_accion_retries_until_success(self):
        intentos = {"count": 0}

        async def accion():
            intentos["count"] += 1
            if intentos["count"] < 3:
                raise Exception("todavia no")
            return "ok"

        resultado = await reintentar_accion("accion", accion, intentos=3, espera=0)

        self.assertEqual(resultado, "ok")
        self.assertEqual(intentos["count"], 3)

    async def test_reintentar_accion_raises_last_error(self):
        async def accion():
            raise RuntimeError("ultimo error")

        with self.assertRaisesRegex(RuntimeError, "ultimo error"):
            await reintentar_accion("accion", accion, intentos=2, espera=0)


if __name__ == "__main__":
    unittest.main()
