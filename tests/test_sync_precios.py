import tempfile
import unittest
from pathlib import Path

import sync_precios


class SyncPreciosExportTests(unittest.TestCase):
    def test_leer_precios_exportados_reads_defontana_html_xls(self):
        contenido = """
        <html><body><table>
          <tr><td>&nbsp;</td><td><font>'A000075</font></td>
              <td>(1 - 1,000)</td><td>2,500</td><td>(0)</td><td>0</td></tr>
          <tr><td>&nbsp;</td><td><font>'A000178</font></td>
              <td>(0.001 - 1,000)</td><td>11,490</td><td>(0)</td><td>0</td></tr>
        </table></body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            archivo = Path(tmp) / "lista.xls"
            archivo.write_text(contenido, encoding="latin-1")
            precios = sync_precios.leer_precios_exportados(archivo)

        self.assertEqual(precios["A000075"], 2500)
        self.assertEqual(precios["A000178"], 11490)

    def test_comparar_precios_exportados_reports_stale_and_missing_codes(self):
        contenido = """
        <html><body><table>
          <tr><td>&nbsp;</td><td>'A000075</td>
              <td>(1 - 1,000)</td><td>2,500</td><td>(0)</td><td>0</td></tr>
        </table></body></html>
        """
        with tempfile.TemporaryDirectory() as tmp:
            archivo = Path(tmp) / "lista.xls"
            archivo.write_text(contenido, encoding="latin-1")
            diferencias = sync_precios.comparar_precios_exportados(
                archivo,
                {"A000075": 2990, "A000178": 11490},
            )

        self.assertEqual(diferencias["A000075"], (2990, 2500))
        self.assertEqual(diferencias["A000178"], (11490, None))


if __name__ == "__main__":
    unittest.main()
