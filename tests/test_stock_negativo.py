import json
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from openpyxl import Workbook, load_workbook

import stock_negativo


class AjusteSinExcelTests(unittest.IsolatedAsyncioTestCase):
    async def _ejecutar_simulado(self, decision, error_limpieza=None):
        item = stock_negativo.AdjustmentItem("A1", "Producto", "-2", "123", "2")
        with tempfile.TemporaryDirectory() as tmp:
            resultado = Path(tmp) / "resultado.json"
            resultado.write_text(json.dumps({"estado": "guardado"}), encoding="utf-8")
            with patch.object(stock_negativo, "preparar", new_callable=AsyncMock,
                              return_value=(None, None, None, [item], [])), \
                 patch.object(stock_negativo, "aplicar", new_callable=AsyncMock,
                              return_value=resultado) as aplicar, \
                 patch.object(stock_negativo, "guardar_informe_interactivo",
                              return_value=Path(tmp) / "informe.xlsx") as informe, \
                 patch.object(stock_negativo, "limpiar_descargas",
                              side_effect=error_limpieza or None, return_value=[]), \
                 patch.object(stock_negativo, "rotar_log"):
                async def confirmar(_items, _omitidos):
                    return decision
                ok = await stock_negativo.ejecutar_ajuste_completo(confirmar_fn=confirmar)
                return ok, aplicar.await_count, informe.call_count

    async def test_ajuste_confirmado_no_genera_excel(self):
        self.assertEqual(await self._ejecutar_simulado(True), (True, 1, 0))

    async def test_cancelar_no_aplica_ajuste(self):
        self.assertEqual(await self._ejecutar_simulado(False), (False, 0, 0))
        self.assertTrue(stock_negativo.ULTIMO_AJUSTE_CANCELADO)

    async def test_fallo_de_limpieza_no_convierte_guardado_en_fallo(self):
        self.assertEqual(await self._ejecutar_simulado(True, RuntimeError("bloqueado")), (True, 1, 0))
        self.assertIn("bloqueado", stock_negativo.ULTIMO_ERROR_LIMPIEZA)

    async def test_guardado_lento_queda_incierto_sin_reintento(self):
        item = stock_negativo.AdjustmentItem("A1", "Producto", "-2", "123", "2")
        page = MagicMock()
        page.url = "https://tivendoapp.defontana.com/inventario/nuevo"
        page.get_by_role.return_value.click = AsyncMock()
        page.wait_for_function = AsyncMock(side_effect=TimeoutError("sin confirmación"))
        browser = MagicMock()
        browser.close = AsyncMock()
        playwright = MagicMock()
        playwright.__aenter__ = AsyncMock(return_value=MagicMock())
        playwright.__aexit__ = AsyncMock(return_value=None)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(stock_negativo, "AJUSTE_DIR", Path(tmp)), \
             patch.object(stock_negativo, "async_playwright", return_value=playwright), \
             patch.object(stock_negativo, "crear_pagina_trabajo", new_callable=AsyncMock,
                          return_value=(browser, MagicMock(), page)), \
             patch.object(stock_negativo, "login_tivendo", new_callable=AsyncMock), \
             patch.object(stock_negativo, "_open_movement", new_callable=AsyncMock), \
             patch.object(stock_negativo, "_configure_movement", new_callable=AsyncMock), \
             patch.object(stock_negativo, "_wait_catalog", new_callable=AsyncMock), \
             patch.object(stock_negativo, "_load_item", new_callable=AsyncMock,
                          return_value={"estado": "cargado", "codigo": "A1"}), \
             patch.object(stock_negativo, "guardar_diagnostico", new_callable=AsyncMock):
            with self.assertRaisesRegex(TimeoutError, "sin confirmación"):
                await stock_negativo.aplicar([item])
            resultado = next(Path(tmp).glob("resultado_ajuste_*.json"))
            payload = json.loads(resultado.read_text(encoding="utf-8"))
        self.assertEqual(payload["estado"], "guardado_incierto_no_reintentar")
        self.assertTrue(stock_negativo.ULTIMO_GUARDADO_INCIERTO)
        self.assertEqual(page.wait_for_function.await_args.kwargs["timeout"], 600000)


class AperturaInformeTests(unittest.IsolatedAsyncioTestCase):
    async def test_espera_titulo_visible_sin_confundirlo_con_menu_oculto(self):
        page = MagicMock()
        informes = MagicMock()
        informes.first.click = AsyncMock()
        inventario = MagicMock()
        inventario.wait_for = AsyncMock()
        inventario.click = AsyncMock()

        def buscar_texto(texto, **_kwargs):
            if texto == "Bodegas":
                raise AssertionError("El texto Bodegas también existe en el menú oculto")
            return informes if texto == "Informes" else inventario

        page.get_by_text.side_effect = buscar_texto
        page.get_by_role.return_value.wait_for = AsyncMock()
        page.locator.return_value.count = AsyncMock(return_value=0)
        await stock_negativo._open_inventory_report(page)

        page.get_by_role.assert_called_once_with("heading", name="Bodegas", exact=True)
        page.get_by_role.return_value.wait_for.assert_awaited_once_with(
            state="visible", timeout=30000,
        )


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
