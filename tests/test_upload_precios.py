import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import upload_precios


class UploadPreciosTests(unittest.TestCase):
    def test_buscar_excel_mas_reciente_returns_newest_xlsx(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            viejo = carpeta / "viejo.xlsx"
            nuevo = carpeta / "nuevo.xlsx"
            ignorado = carpeta / "ignorado.txt"
            viejo.write_text("1", encoding="utf-8")
            nuevo.write_text("2", encoding="utf-8")
            ignorado.write_text("3", encoding="utf-8")
            viejo.touch()
            nuevo.touch()
            viejo_mtime = 1000
            nuevo_mtime = 2000
            import os
            os.utime(viejo, (viejo_mtime, viejo_mtime))
            os.utime(nuevo, (nuevo_mtime, nuevo_mtime))

            resultado = upload_precios.buscar_excel_mas_reciente(str(carpeta))

        self.assertEqual(resultado.name, "nuevo.xlsx")

    def test_buscar_excel_mas_reciente_exits_when_folder_missing(self):
        with patch.object(upload_precios, "log"):
            with self.assertRaises(SystemExit):
                upload_precios.buscar_excel_mas_reciente(r"C:\ruta\que\no\existe")

    def test_buscar_excel_mas_reciente_exits_when_no_xlsx(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "archivo.txt").write_text("sin excel", encoding="utf-8")
            with patch.object(upload_precios, "log"):
                with self.assertRaises(SystemExit):
                    upload_precios.buscar_excel_mas_reciente(tmp)


if __name__ == "__main__":
    unittest.main()
