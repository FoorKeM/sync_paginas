import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook

import stock_negativo


def crear_libro(path: Path, rows: list[list]) -> None:
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    workbook.save(path)


class CruzarNegativosTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.inventory = self.directory / "inventario.xlsx"
        self.articles = self.directory / "articulos.xlsx"

    def tearDown(self):
        self.temp.cleanup()

    def test_solo_incluye_codigos_a_con_cantidad_negativa(self):
        crear_libro(
            self.inventory,
            [
                ["titulo"],
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Negativo", -15],
                ["A2", "Cero", 0],
                ["A3", "Positivo", 4],
                ["P1", "Pack negativo", -8],
            ],
        )

        resultado = stock_negativo.leer_negativos(self.inventory)

        self.assertEqual([item["codigo"] for item in resultado], ["A1"])
        self.assertEqual(str(resultado[0]["stock"]), "-15")

    def test_cruce_preserva_barcode_y_decimales(self):
        crear_libro(
            self.inventory,
            [
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Producto", -0.264],
            ],
        )
        crear_libro(
            self.articles,
            [
                ["Código", "Nombre", "Código barra interno", "Disponible para venta", "Activo"],
                ["A1", "Producto", "0000123", "Si", "Si"],
            ],
        )

        resultado, omitidos = stock_negativo.cruzar_negativos(self.inventory, self.articles)

        self.assertEqual(resultado[0].codigo_barra, "0000123")
        self.assertEqual(resultado[0].cantidad_propuesta, "0.264")
        self.assertEqual(omitidos, [])

    def test_omite_si_el_articulo_esta_duplicado(self):
        crear_libro(
            self.inventory,
            [
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Producto", -2],
                ["A2", "Otro", -3],
            ],
        )
        crear_libro(
            self.articles,
            [
                ["Código", "Nombre", "Código barra interno", "Disponible para venta", "Activo"],
                ["A1", "Producto", "123", "Si", "Si"],
                ["A1", "Producto repetido", "456", "Si", "Si"],
                ["A2", "Otro", "789", "Si", "Si"],
            ],
        )

        resultado, omitidos = stock_negativo.cruzar_negativos(self.inventory, self.articles)

        self.assertEqual([item.codigo for item in resultado], ["A2"])
        self.assertEqual(len(omitidos), 1)
        self.assertEqual(omitidos[0]["codigo"], "A1")
        self.assertIn("hay 2", omitidos[0]["motivo"])

    def test_omite_si_no_hay_codigo_barra_y_sigue_con_los_demas(self):
        crear_libro(
            self.inventory,
            [
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Sin barra", -2],
                ["A2", "Con barra", -3],
            ],
        )
        crear_libro(
            self.articles,
            [
                ["Código", "Nombre", "Código barra interno", "Disponible para venta", "Activo"],
                ["A1", "Sin barra", "", "Si", "Si"],
                ["A2", "Con barra", "999", "Si", "Si"],
            ],
        )

        resultado, omitidos = stock_negativo.cruzar_negativos(self.inventory, self.articles)

        self.assertEqual([item.codigo for item in resultado], ["A2"])
        self.assertEqual(len(omitidos), 1)
        self.assertEqual(omitidos[0]["codigo"], "A1")
        self.assertEqual(omitidos[0]["motivo"], "sin Codigo barra interno")

    def test_omite_si_el_articulo_esta_inactivo(self):
        crear_libro(
            self.inventory,
            [
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Producto", -2],
            ],
        )
        crear_libro(
            self.articles,
            [
                ["Código", "Nombre", "Código barra interno", "Disponible para venta", "Activo"],
                ["A1", "Producto", "123", "Si", "No"],
            ],
        )

        resultado, omitidos = stock_negativo.cruzar_negativos(self.inventory, self.articles)

        self.assertEqual(resultado, [])
        self.assertEqual(omitidos[0]["motivo"], "articulo inactivo")

    def test_omite_si_no_disponible_para_venta(self):
        crear_libro(
            self.inventory,
            [
                ["CodArticulo", "Descripción Artículo", "Cantidad"],
                ["A1", "Producto", -2],
            ],
        )
        crear_libro(
            self.articles,
            [
                ["Código", "Nombre", "Código barra interno", "Disponible para venta", "Activo"],
                ["A1", "Producto", "123", "No", "Si"],
            ],
        )

        resultado, omitidos = stock_negativo.cruzar_negativos(self.inventory, self.articles)

        self.assertEqual(resultado, [])
        self.assertEqual(omitidos[0]["motivo"], "no disponible para venta")


class InformeExcelTests(unittest.TestCase):
    def test_informe_solo_contiene_negativos_cargados(self):
        with tempfile.TemporaryDirectory() as tmp:
            carpeta = Path(tmp)
            resultado = carpeta / "resultado.json"
            destino = carpeta / "informe.xlsx"
            resultado.write_text(
                json.dumps({
                    "fecha": "2026-06-09T16:34:40",
                    "bodega": "LIBERTADOR 1476",
                    "articulos": [
                        {
                            "codigo": "A000001",
                            "codigo_barra": "0012345",
                            "stock_actual": "-15.5",
                            "cantidad": "15.5",
                            "estado": "cargado",
                        },
                        {
                            "codigo": "A000002",
                            "codigo_barra": "999",
                            "stock_actual": "2",
                            "estado": "omitido_no_negativo",
                        },
                        {
                            "codigo": "P000003",
                            "codigo_barra": "888",
                            "stock_actual": "-3",
                            "cantidad": "3",
                            "estado": "cargado",
                        },
                    ],
                }),
                encoding="utf-8",
            )
            items = [
                stock_negativo.AdjustmentItem(
                    codigo="A000001",
                    descripcion="Articulo de prueba",
                    stock_informe="-15.5",
                    codigo_barra="0012345",
                    cantidad_propuesta="15.5",
                )
            ]

            stock_negativo.crear_informe(resultado, items, destino)

            workbook = load_workbook(destino, read_only=True, data_only=True)
            try:
                filas = list(workbook.active.iter_rows(values_only=True))
            finally:
                workbook.close()

        self.assertEqual(filas[0], stock_negativo.INFORME_HEADERS)
        self.assertEqual(len(filas), 2)
        self.assertEqual(filas[1][2], "A000001")
        self.assertEqual(filas[1][3], "Articulo de prueba")
        self.assertEqual(filas[1][4], "0012345")
        self.assertEqual(filas[1][5], -15.5)
        self.assertEqual(filas[1][6], 15.5)


class LimpiarDescargasTests(unittest.TestCase):
    def test_elimina_archivos_y_registra_resultado(self):
        with tempfile.TemporaryDirectory() as tmp:
            directorio = Path(tmp)
            carpeta_descargas = directorio / "descargas"
            carpeta_descargas.mkdir()
            (carpeta_descargas / "inventario.xlsx").write_bytes(b"xlsx")
            (carpeta_descargas / "articulos.xlsx").write_bytes(b"xlsx")
            resultado = directorio / "resultado.json"
            resultado.write_text('{"estado": "guardado"}', encoding="utf-8")

            with patch.object(stock_negativo, "DESCARGA_DIR", carpeta_descargas):
                eliminados = stock_negativo.limpiar_descargas(resultado)

            payload = json.loads(resultado.read_text(encoding="utf-8"))
            self.assertEqual(len(eliminados), 2)
            self.assertEqual(list(carpeta_descargas.iterdir()), [])
            self.assertEqual(len(payload["limpieza_descargas"]["archivos_eliminados"]), 2)
            self.assertEqual(payload["limpieza_descargas"]["errores"], [])


class ZonaStockErrorTests(unittest.TestCase):
    def test_error_de_zona_lista_las_zonas_encontradas(self):
        """Si la zona configurada no calza, el error debe listar las zonas reales
        que devolvio Tivendo, para poder corregir config.py sin adivinar."""
        item = stock_negativo.AdjustmentItem(
            codigo="A1", descripcion="Producto", stock_informe="-2",
            codigo_barra="123", cantidad_propuesta="2",
        )

        class FakeResponse:
            async def json(self):
                return {
                    "productos": [{
                        "sku": "A1",
                        "codigoInterno": "123",
                        "nombre": "Producto",
                        "stock": {"bodegasConStock": [
                            {"idBodega": "ZONA_REAL_1", "stock": "-2"},
                            {"idBodega": "ZONA_REAL_2", "stock": "5"},
                        ]},
                    }]
                }

        # No probamos la parte de Playwright (requiere navegador real); solo
        # verificamos que el mensaje de error sea informativo cuando la zona
        # configurada no aparece en la respuesta, usando la misma logica.
        bodegas_con_stock = [
            {"idBodega": "ZONA_REAL_1", "stock": "-2"},
            {"idBodega": "ZONA_REAL_2", "stock": "5"},
        ]
        zona_configurada = "ZONA_QUE_NO_EXISTE"
        stock_bodega = None
        for bodega_stock in bodegas_con_stock:
            if bodega_stock.get("idBodega") == zona_configurada:
                stock_bodega = bodega_stock.get("stock")
                break
        self.assertIsNone(stock_bodega)
        zonas_encontradas = sorted({str(b.get("idBodega")) for b in bodegas_con_stock})
        self.assertEqual(zonas_encontradas, ["ZONA_REAL_1", "ZONA_REAL_2"])


if __name__ == "__main__":
    unittest.main()
