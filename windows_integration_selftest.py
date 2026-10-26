#!/usr/bin/env python3
"""验证文件右键菜单的安装、重复安装与移除；注册表测试使用独立临时分支。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

import windows_integration as integration
from txt_encoder_core import make_console_tolerant


class RegistrationPlanTests(unittest.TestCase):
    def test_command_supports_spaces_and_shell_characters(self):
        with tempfile.TemporaryDirectory(prefix="toolbox & ! ") as temp:
            app = Path(temp) / "工具箱 % 文件"
            app.mkdir()
            (app / "file_toolbox.py").touch()
            values = integration.registration_entries(app_dir=app, python_exe=sys.executable)
            command = values[integration.PROG_ID + r"\shell\open\command"][""]
            self.assertEqual(command, f'"{Path(sys.executable).resolve()}" "{app / "file_toolbox.py"}" "%1"')
            self.assertNotIn("cmd.exe", command.lower())

    def test_missing_program_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                integration.registration_entries(app_dir=temp)


@unittest.skipUnless(os.name == "nt", "Windows 注册表测试")
class RegistryTests(unittest.TestCase):
    def setUp(self):
        import winreg
        self.reg = winreg
        self.base = rf"Software\TxtEncoderIntegrationTest_{uuid.uuid4().hex}"
        self.root = self.base + r"\Classes"

    def tearDown(self):
        integration._delete_tree(self.reg, self.base)

    def put(self, path, name, value):
        with self.reg.CreateKeyEx(self.reg.HKEY_CURRENT_USER, self.root + "\\" + path) as key:
            self.reg.SetValueEx(key, name, 0, self.reg.REG_SZ, value)

    def get(self, path, name=""):
        with self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, self.root + "\\" + path) as key:
            return self.reg.QueryValueEx(key, name)[0]

    def test_install_update_uninstall_preserves_other_apps(self):
        self.put(".txt", "", "Original.TextEditor")
        self.put(r".txt\OpenWithProgids", "Other.App", "")
        other_verb = r"SystemFileAssociations\.txt\shell\OtherTool\command"
        self.put(other_verb, "", "other.exe")
        self.assertFalse(integration.is_installed(root_path=self.root))
        for _ in range(2):
            self.assertEqual(integration.install(root_path=self.root), len(integration.EXTENSIONS))
            self.assertTrue(integration.is_installed(root_path=self.root))
        self.assertEqual(self.get(".txt"), "Original.TextEditor")
        self.assertEqual(self.get(other_verb), "other.exe")
        self.assertEqual(self.get(r".txt\OpenWithProgids", "Other.App"), "")
        for ext in (".txt", ".zip", ".rar", ".7z", ".001"):
            verb = rf"SystemFileAssociations\{ext}\shell\{integration.VERB}"
            self.assertEqual(self.get(verb), integration.MENU_LABEL)
            self.assertTrue(self.get(verb + r"\command").endswith(' "%1"'))
            self.assertEqual(self.get(ext + r"\OpenWithProgids", integration.PROG_ID), "")
        integration.uninstall(root_path=self.root)
        integration.uninstall(root_path=self.root)
        self.assertFalse(integration.is_installed(root_path=self.root))
        self.assertEqual(self.get(".txt"), "Original.TextEditor")
        self.assertEqual(self.get(other_verb), "other.exe")
        self.assertEqual(self.get(r".txt\OpenWithProgids", "Other.App"), "")
        with self.assertRaises(FileNotFoundError):
            self.get(r".txt\OpenWithProgids", integration.PROG_ID)

    def test_reinstall_updates_moved_program_path(self):
        integration.install(root_path=self.root)
        with tempfile.TemporaryDirectory() as temp:
            moved = Path(temp) / "新路径 含空格"
            moved.mkdir()
            (moved / "file_toolbox.py").touch()
            integration.install(app_dir=moved, root_path=self.root)
            self.assertFalse(integration.is_installed(root_path=self.root))
            self.assertTrue(integration.is_installed(app_dir=moved, root_path=self.root))
            self.assertIn(str(moved), self.get(integration.PROG_ID + r"\shell\open\command"))

    def test_invalid_program_does_not_write_registry(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(FileNotFoundError):
                integration.install(app_dir=temp, root_path=self.root)
        with self.assertRaises(FileNotFoundError):
            self.reg.OpenKey(self.reg.HKEY_CURRENT_USER, self.base)


if __name__ == "__main__":
    make_console_tolerant()
    suite = unittest.defaultTestLoader.loadTestsFromModule(sys.modules[__name__])
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if result.wasSuccessful():
        print("文件右键菜单自检全部通过 ✔")
    raise SystemExit(0 if result.wasSuccessful() else 1)
