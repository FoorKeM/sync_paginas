import unittest

import notificaciones_whatsapp


class WhatsAppMessageTests(unittest.TestCase):
    def test_detecta_lista_de_participantes_como_nombre_invalido(self):
        self.assertTrue(
            notificaciones_whatsapp._parece_lista_participantes(
                "Encargado, +56 9 1111 1111, +56 9 2222 2222, Tú"
            )
        )
        self.assertFalse(
            notificaciones_whatsapp._parece_lista_participantes(
                "Sistemas Mercadohouse"
            )
        )

    def test_construir_mensaje_exitoso(self):
        mensaje = notificaciones_whatsapp.construir_mensaje(
            {
                "ok": True,
                "sucursal": "LIBERTADOR 1476",
                "hora_fin": "21:34",
                "duracion": "4m 32s",
                "precios": {"total": 25, "ok": 25, "fallidos": []},
                "pasos": [
                    {"nombre": "Sync Artículos Tivendo POS → MH", "ok": True},
                    {"nombre": "Sync Packs     Tivendo POS → MH", "ok": True},
                    {"nombre": "Sync Precios   Tivendo ERP → MH", "ok": True},
                ],
            }
        )

        self.assertIn("SINCRONIZACIÓN NOCTURNA CORRECTA", mensaje)
        self.assertIn("Precios: 25 de 25", mensaje)
        self.assertIn("Artículos: OK", mensaje)
        self.assertIn("Packs: OK", mensaje)
        self.assertIn("Mercadohouse: OK", mensaje)

    def test_construir_mensaje_con_fallos(self):
        mensaje = notificaciones_whatsapp.construir_mensaje(
            {
                "ok": False,
                "sucursal": "LIBERTADOR 1476",
                "hora_fin": "21:34",
                "duracion": "4m 32s",
                "precios": {
                    "total": 25,
                    "ok": 21,
                    "fallidos": ["A000012", "A000005"],
                },
                "pasos": [
                    {"nombre": "Sync Precios   Tivendo ERP → MH", "ok": False},
                ],
                "etapa_fallida": "Sync Precios Tivendo ERP → MH",
            }
        )

        self.assertIn("FALLÓ SINCRONIZACIÓN NOCTURNA", mensaje)
        self.assertIn("Precios: 21 de 25", mensaje)
        self.assertIn("A000012", mensaje)
        self.assertIn("A000005", mensaje)
        self.assertIn("Etapa: Sync Precios Tivendo ERP → MH", mensaje)


if __name__ == "__main__":
    unittest.main()
