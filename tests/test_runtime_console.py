import ctypes
import unittest
from unittest.mock import MagicMock, patch

import runtime_console


class RuntimeConsoleTests(unittest.TestCase):
    def test_disables_quick_edit_without_clearing_other_console_flags(self):
        kernel32 = MagicMock()
        kernel32.GetStdHandle.return_value = 1

        def leer_modo(_entrada, puntero):
            ctypes.cast(puntero, ctypes.POINTER(ctypes.c_uint)).contents.value = 0x0041
            return 1

        kernel32.GetConsoleMode.side_effect = leer_modo
        kernel32.SetConsoleMode.return_value = 1
        with patch.object(runtime_console.os, "name", "nt"), \
             patch("ctypes.WinDLL", return_value=kernel32):
            self.assertTrue(runtime_console.preparar_consola())
        kernel32.SetConsoleMode.assert_called_once_with(1, 0x0081)


if __name__ == "__main__":
    unittest.main()
