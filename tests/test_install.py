"""全局 ``remote`` 入口安装器单元测试。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from remote_plugin import cli
from remote_plugin.install import InstallError, install_launcher


class TestInstallLauncher(unittest.TestCase):
    """覆盖首次安装、幂等重入与 fail-closed 占用保护。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.source = root / "remote"
        self.source.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.bin_dir = root / "bin"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_creates_absolute_symlink_atomically(self):
        result = install_launcher(self.source, self.bin_dir)
        target = self.bin_dir / "remote"
        self.assertEqual(result.install_path, target)
        self.assertEqual(result.command_name, "remote")
        self.assertFalse(result.already_exists)
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), self.source.resolve())
        self.assertEqual(result.to_dict()["status"], "ready")

    def test_existing_plugin_link_is_idempotent(self):
        first = install_launcher(self.source, self.bin_dir)
        second = install_launcher(self.source, self.bin_dir)
        self.assertFalse(first.already_exists)
        self.assertTrue(second.already_exists)
        self.assertEqual(second.install_path, first.install_path)

    def test_existing_regular_file_fails_closed(self):
        self.bin_dir.mkdir(parents=True)
        target = self.bin_dir / "remote"
        target.write_text("user file\n", encoding="utf-8")
        with self.assertRaises(InstallError):
            install_launcher(self.source, self.bin_dir)
        self.assertEqual(target.read_text(encoding="utf-8"), "user file\n")

    def test_existing_other_symlink_fails_closed(self):
        self.bin_dir.mkdir(parents=True)
        other = Path(self.tmp.name) / "other"
        other.write_text("other\n", encoding="utf-8")
        target = self.bin_dir / "remote"
        os.symlink(other, target)
        with self.assertRaises(InstallError):
            install_launcher(self.source, self.bin_dir)
        self.assertEqual(target.resolve(), other.resolve())

    def test_broken_symlink_fails_closed(self):
        self.bin_dir.mkdir(parents=True)
        target = self.bin_dir / "remote"
        os.symlink(Path(self.tmp.name) / "missing", target)
        with self.assertRaises(InstallError):
            install_launcher(self.source, self.bin_dir)
        self.assertTrue(target.is_symlink())

    def test_missing_source_fails(self):
        with self.assertRaises(InstallError):
            install_launcher(Path(self.tmp.name) / "missing", self.bin_dir)


class TestInstallCli(unittest.TestCase):
    """确保子命令已注册且 handler 遵循统一 JSON 结果约定。"""

    def test_parser_and_dispatch_registration(self):
        args = cli.build_parser().parse_args(["install"])
        self.assertEqual(args.command, "install")
        self.assertIn("install", cli.COMMANDS)

    def test_cli_handler_returns_install_result(self):
        payload = {"status": "ready", "install_path": "/tmp/remote"}
        with mock.patch("remote_plugin.install.install_launcher") as installer:
            installer.return_value.to_dict.return_value = payload
            from remote_plugin.install import cli_install

            self.assertEqual(cli_install(SimpleNamespace()), payload)
            installer.assert_called_once()


if __name__ == "__main__":
    unittest.main()
