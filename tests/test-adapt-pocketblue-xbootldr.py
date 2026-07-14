#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "adapt-pocketblue-xbootldr"
TOOLS = ("mkfs.ext4", "debugfs", "e2fsck")
HAVE_TOOLS = all(shutil.which(tool) for tool in TOOLS)
LOADER = importlib.machinery.SourceFileLoader("adapt_pocketblue_xbootldr", str(TOOL))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
ADAPTER = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(ADAPTER)
OLD_ENTRY = """title Fedora PocketBlue
version 6.18-test
linux /ostree/pocketblue/vmlinuz
initrd /ostree/pocketblue/initramfs.img
fdtdir /dtb
options root=UUID=ROOT rootflags=subvol=/root boot=UUID=BOOT ostree=/ostree/boot.1/pocketblue/0 rw quiet console=ttyS0,115200 console=tty0 rd.lvm.lv=old/root rd.lvm.lv=old/boot sysrq_always_enabled=0
"""
EXPECTED_OPTIONS = (
    "root=UUID=ROOT rootflags=subvol=/root boot=UUID=BOOT "
    "ostree=/ostree/boot.1/pocketblue/0 rw quiet "
    "console=tty0 console=ttyMSM0,115200n8 "
    "rd.lvm.lv=franken/pocketblue-root "
    "rd.lvm.lv=franken/pocketblue-xbootldr "
    "rd.lvm.lv=franken/pocketblue-esp sysrq_always_enabled=1"
)


def run(command: list[str], success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if success and result.returncode != 0:
        raise AssertionError(f"command failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not success and result.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {command}")
    return result


def debugfs(image: Path, request: str, write: bool = False) -> str:
    command = ["debugfs"]
    if write:
        command.append("-w")
    command.extend(["-R", request, str(image)])
    return run(command).stdout


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Fixture:
    def __init__(self, root: Path, entries: list[tuple[str, str]] | None = None):
        self.root = root
        self.image = root / "fedora_boot.raw"
        with self.image.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024)
        run(["mkfs.ext4", "-q", "-F", str(self.image)])
        debugfs(self.image, "mkdir /loader", write=True)
        debugfs(self.image, "mkdir /loader/entries", write=True)
        debugfs(self.image, "mkdir /ostree", write=True)
        debugfs(self.image, "mkdir /ostree/pocketblue", write=True)
        debugfs(self.image, "mkdir /dtb", write=True)
        for name in ("vmlinuz", "initramfs.img"):
            artifact = root / f"host-{name}"
            artifact.write_bytes(f"fixture-{name}".encode())
            debugfs(
                self.image,
                f"write {artifact} /ostree/pocketblue/{name}",
                write=True,
            )
        for name, contents in entries or [("ostree-1.conf", OLD_ENTRY)]:
            host = root / f"host-{name}"
            host.write_text(contents)
            debugfs(self.image, f"write {host} /loader/entries/{name}", write=True)

    def entry(self, name: str = "ostree-1.conf") -> str:
        return debugfs(self.image, f"cat /loader/entries/{name}")

    def command(self, output: Path) -> list[str]:
        return [str(TOOL), "--source", str(self.image), "--output", str(output)]


@unittest.skipUnless(HAVE_TOOLS, "mkfs.ext4/debugfs/e2fsck are required")
class AdaptPocketBlueTests(unittest.TestCase):
    def test_sparse_copy_fallback_rewinds_destination_after_partial_progress(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.raw"
            destination = root / "destination.raw"
            with source.open("wb") as stream:
                stream.write(b"A" * 4096)
                stream.seek(1024 * 1024)
                stream.write(b"B" * 4096)
            destination_fd = os.open(destination, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
            real_lseek = os.lseek
            data_lookups = 0

            def flaky_lseek(fd: int, offset: int, whence: int) -> int:
                nonlocal data_lookups
                if whence == os.SEEK_DATA:
                    data_lookups += 1
                    if data_lookups == 2:
                        raise OSError(22, "forced SEEK_DATA fallback")
                return real_lseek(fd, offset, whence)

            try:
                with mock.patch.object(ADAPTER.os, "lseek", side_effect=flaky_lseek):
                    ADAPTER.sparse_copy(source, destination_fd)
            finally:
                os.close(destination_fd)
            self.assertGreaterEqual(data_lookups, 2)
            self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_adapts_sparse_copy_exactly_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            source_hash = sha256(fixture.image)
            source_entry = fixture.entry()
            output = root / "adapted.raw"
            result = run(fixture.command(output))

            self.assertEqual(sha256(fixture.image), source_hash)
            self.assertEqual(fixture.entry(), source_entry)
            self.assertTrue(output.is_file())
            self.assertLess(output.stat().st_blocks * 512, output.stat().st_size // 2)
            reported_hash, reported_path = result.stdout.strip().split("  ", 1)
            self.assertEqual(reported_hash, sha256(output))
            self.assertEqual(Path(reported_path), output)

            entry = debugfs(output, "cat /loader/entries/ostree-1.conf")
            self.assertIn("title PocketBlue sdm670 Phosh (frankensargo LVM)\n", entry)
            self.assertIn("linux /ostree/pocketblue/vmlinuz\n", entry)
            self.assertIn("initrd /ostree/pocketblue/initramfs.img\n", entry)
            self.assertIn("fdtdir /dtb\n", entry)
            self.assertIn(f"options {EXPECTED_OPTIONS}\n", entry)
            self.assertEqual(entry.count("console="), 2)
            self.assertEqual(entry.count("rd.lvm.lv="), 3)
            self.assertEqual(entry.count("sysrq_always_enabled="), 1)
            run(["e2fsck", "-fn", str(output)])

    def test_refuses_existing_output_without_changing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            output = root / "adapted.raw"
            output.write_bytes(b"sentinel")
            result = run(fixture.command(output), success=False)
            self.assertIn("output exists", result.stderr)
            self.assertEqual(output.read_bytes(), b"sentinel")

    def test_rejects_ambiguous_or_incomplete_entries_without_output(self):
        cases = (
            (
                [("one.conf", OLD_ENTRY), ("two.conf", OLD_ENTRY)],
                "expected exactly one",
            ),
            (
                [("one.conf", OLD_ENTRY.replace("fdtdir /dtb\n", ""))],
                "lacks required field",
            ),
            (
                [("one.conf", OLD_ENTRY.replace(" boot=UUID=BOOT", ""))],
                "exactly one nonempty boot=",
            ),
        )
        for index, (entries, diagnostic) in enumerate(cases):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                fixture = Fixture(root, entries)
                source_hash = sha256(fixture.image)
                output = root / "adapted.raw"
                result = run(fixture.command(output), success=False)
                self.assertIn(diagnostic, result.stderr)
                self.assertFalse(output.exists())
                self.assertEqual(sha256(fixture.image), source_hash)

    def test_rejects_missing_or_wrong_type_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            debugfs(fixture.image, "rm /ostree/pocketblue/vmlinuz", write=True)
            output = root / "missing.raw"
            result = run(fixture.command(output), success=False)
            self.assertIn("linux artifact", result.stderr)
            self.assertIn("is missing", result.stderr)
            self.assertFalse(output.exists())

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            debugfs(fixture.image, "rmdir /dtb", write=True)
            host = root / "host-dtb-file"
            host.write_bytes(b"not-a-directory")
            debugfs(fixture.image, f"write {host} /dtb", write=True)
            output = root / "wrong-type.raw"
            result = run(fixture.command(output), success=False)
            self.assertIn("fdtdir artifact", result.stderr)
            self.assertIn("is regular, expected directory", result.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
