import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

import menu
import upload_precios


class VistaPreviaPreciosTests(unittest.TestCase):
    def test_preview_shows_catalog_name_and_chilean_price_without_barcode_or_ranges(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            precios = carpeta / "precios.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "RangoInicial1", "RangoFinal1", "Precio1",
                         "RangoInicial2", "RangoFinal2", "Precio2"])
            hoja.append(["A014819", 1, 9999, 990, None, None, None])
            hoja.append(["P000631", 1, 9999, 11880, None, None, None])
            libro.save(precios)

            catalogo = carpeta / "Listado_De_Articulos.xlsx"
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "Nombre", "Código barra interno"])
            hoja.append(["A014819", "PAPEL HIGIÉNICO SCOTT", "000123"])
            libro.save(catalogo)

            salida = StringIO()
            with patch.object(upload_precios, "DESCARGA_DIR", carpeta), \
                 patch.object(upload_precios, "USER_DATA_DIR", carpeta), \
                 patch("builtins.input", return_value="n"), redirect_stdout(salida):
                self.assertFalse(menu.mostrar_resumen_precios_y_confirmar(str(precios)))
            texto = salida.getvalue()
            self.assertIn("A014819 | PAPEL HIGIÉNICO SCOTT", texto)
            self.assertIn("Precio: $990", texto)
            self.assertIn("Precio: $11.880", texto)
            self.assertIn("P000631 | Nombre no disponible", texto)
            self.assertNotIn("Código de barras", texto)
            self.assertNotIn("Rango 1:", texto)

    def test_price_format_uses_clp_thousands_separator(self):
        self.assertEqual(menu.formatear_precio_clp("990"), "$990")
        self.assertEqual(menu.formatear_precio_clp("11880"), "$11.880")
        self.assertEqual(menu.formatear_precio_clp("1234567"), "$1.234.567")
        self.assertEqual(menu.formatear_precio_clp("990.50"), "$990,5")

    def test_catalog_lookup_uses_older_export_for_codes_missing_in_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            for nombre, codigo, articulo in (
                ("Listado_viejo_articulos.xlsx", "A1", "Artículo anterior"),
                ("Listado_nuevo_articulos.xlsx", "A2", "Artículo reciente"),
            ):
                libro = Workbook()
                hoja = libro.active
                hoja.append(["Código", "Nombre", "Código barra interno"])
                hoja.append([codigo, articulo, "123"])
                libro.save(carpeta / nombre)
            detalles = upload_precios.detalles_productos_para_vista_previa({"A1", "A2"}, carpeta)
            self.assertEqual(detalles["A1"]["nombre"], "Artículo anterior")
            self.assertEqual(detalles["A2"]["nombre"], "Artículo reciente")

    def test_pack_code_uses_pack_export_instead_of_article_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "Nombre", "Código barra interno"])
            hoja.append(["P000631", "Nombre equivocado en artículos", "111"])
            libro.save(carpeta / "Listado_De_Articulos.xlsx")

            libro = Workbook()
            hoja = libro.active
            hoja.append(["Código", "Nombre", "Precio", "Código barra interno",
                         "Código producto en pack", "Cantidad producto en pack"])
            hoja.append(["P000631", "PACK DE PRUEBA", 11880, "000456", "A014819", 2])
            libro.save(carpeta / "Maestro_de_Packs.xlsx")

            detalles = upload_precios.detalles_productos_para_vista_previa({"P000631"}, carpeta)
            self.assertEqual(detalles["P000631"], {"nombre": "PACK DE PRUEBA"})

    def test_lookup_reads_downloads_catalog_with_damaged_code_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            descargas = carpeta / "Downloads"
            descargas.mkdir()
            libro = Workbook()
            hoja = libro.active
            hoja.append(["LISTADO DE ARTICULOS", None])
            hoja.append(["C�digo", "Nombre"])
            hoja.append(["A014819", "PAPEL SCOTT"])
            libro.save(descargas / "Listado_De_Articulos.xlsx")
            with patch.object(upload_precios, "DESCARGA_DIR", carpeta / "interno"), \
                 patch.object(upload_precios, "USER_DATA_DIR", carpeta / "datos"), \
                 patch.object(upload_precios.Path, "home", return_value=carpeta):
                detalles = upload_precios.detalles_productos_para_vista_previa({"A014819"})
            self.assertEqual(detalles["A014819"]["nombre"], "PAPEL SCOTT")


if __name__ == "__main__":
    unittest.main()
