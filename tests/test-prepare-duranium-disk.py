#!/usr/bin/env python3

from __future__ import annotations

import binascii
import hashlib
import json
import os
import runpy
import shutil
import stat
import struct
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "prepare-duranium-disk"
TOOL_MODULE = runpy.run_path(str(TOOL))
SECTOR = 512
DISK_SECTORS = 150000
DISK_BYTES = DISK_SECTORS * SECTOR
ESP_START = 2048
ESP_SECTORS = 131072
ESP_BYTES = ESP_SECTORS * SECTOR
DISK_GUID = uuid.UUID("eb5e7a01-f599-4065-b5c8-4f715b6a6d39")
ESP_UUID = uuid.UUID("120c6e48-0d10-4817-94d2-31dd39e8a4cf")
EFI_GUID = uuid.UUID("c12a7328-f81f-11d2-ba4b-00a0c93ec93b")
UKI_NAME = "google-sargo_phosh_edge_26070701.efi"
CMDLINE = (
    "quiet splash plymouth.ignore-serial-consoles "
    "usrhash=a82ab03b602065101dcf0c47fd709bc03f2e2edd47dd82897cff0ed539ecf627 rw"
)
EPOCH = 315532800


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as file:
        while block := file.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


def align(value: int, boundary: int) -> int:
    return (value + boundary - 1) // boundary * boundary


def make_uki() -> bytes:
    sections = [
        (b".cmdline", CMDLINE.encode() + b"\0"),
        (b".linux", b"synthetic-arm64-kernel"),
        (b".initrd", b"synthetic-initrd"),
        (b".profile", b"ID=default\n"),
    ]
    pe_offset = 64
    optional_size = 240
    table = pe_offset + 24 + optional_size
    raw_offset = align(table + len(sections) * 40, 512)
    output = bytearray(raw_offset)
    output[:2] = b"MZ"
    struct.pack_into("<I", output, 60, pe_offset)
    output[pe_offset : pe_offset + 4] = b"PE\0\0"
    struct.pack_into(
        "<HHIIIHH", output, pe_offset + 4, 0xAA64, len(sections), 0, 0, 0, optional_size, 0
    )
    struct.pack_into("<H", output, pe_offset + 24, 0x20B)
    cursor = raw_offset
    for index, (name, contents) in enumerate(sections):
        padded = align(len(contents), 512)
        header = table + index * 40
        output[header : header + 8] = name.ljust(8, b"\0")
        struct.pack_into("<IIII", output, header + 8, len(contents), cursor, padded, cursor)
        output.extend(contents)
        output.extend(b"\0" * (padded - len(contents)))
        cursor += padded
    return bytes(output)


def append_newc(output: bytearray, name: str, mode: int, contents: bytes, inode: int) -> None:
    encoded = name.encode() + b"\0"
    fields = (inode, mode, 0, 0, 1, 0, len(contents), 0, 0, 0, 0, len(encoded), 0)
    output.extend(b"070701" + b"".join(f"{field:08x}".encode() for field in fields))
    output.extend(encoded)
    output.extend(b"\0" * (-len(output) % 4))
    output.extend(contents)
    output.extend(b"\0" * (-len(output) % 4))


def make_adapter() -> bytes:
    output = bytearray()
    append_newc(output, "init", stat.S_IFREG | 0o755, b"#!/bin/sh\n", 1)
    manifest = json.dumps(
        {"format": "frankensargo.duranium-lvm-adapter.v1", "fixture": True},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    append_newc(
        output,
        "usr/lib/frankensargo-duranium/build-manifest.json",
        stat.S_IFREG | 0o644,
        manifest,
        2,
    )
    append_newc(output, "TRAILER!!!", 0, b"", 3)
    output.extend(b"\0" * (-len(output) % 512))
    return bytes(output)


def gpt_header(
    current: int,
    backup: int,
    entries_lba: int,
    entries_crc: int,
    last_usable: int,
) -> bytes:
    header = bytearray(SECTOR)
    struct.pack_into(
        "<8sIIIIQQQQ16sQIII",
        header,
        0,
        b"EFI PART",
        0x00010000,
        92,
        0,
        0,
        current,
        backup,
        34,
        last_usable,
        DISK_GUID.bytes_le,
        entries_lba,
        128,
        128,
        entries_crc,
    )
    struct.pack_into("<I", header, 16, binascii.crc32(header[:92]) & 0xFFFFFFFF)
    return bytes(header)


def write_gpt(path: Path) -> None:
    entries = bytearray(128 * 128)
    entry = bytearray(128)
    entry[:16] = EFI_GUID.bytes_le
    entry[16:32] = ESP_UUID.bytes_le
    struct.pack_into("<QQQ", entry, 32, ESP_START, ESP_START + ESP_SECTORS - 1, 0)
    name = "esp".encode("utf-16le")
    entry[56 : 56 + len(name)] = name
    entries[:128] = entry
    entries_crc = binascii.crc32(entries) & 0xFFFFFFFF
    backup_lba = DISK_SECTORS - 1
    backup_entries = backup_lba - len(entries) // SECTOR
    last_usable = backup_entries - 1
    mbr = bytearray(SECTOR)
    struct.pack_into(
        "<B3sB3sII",
        mbr,
        446,
        0,
        b"\0\0\0",
        0xEE,
        b"\0\0\0",
        1,
        DISK_SECTORS - 1,
    )
    mbr[510:512] = b"\x55\xaa"
    with path.open("r+b", buffering=0) as file:
        file.write(mbr)
        file.seek(SECTOR)
        file.write(gpt_header(1, backup_lba, 2, entries_crc, last_usable))
        file.seek(2 * SECTOR)
        file.write(entries)
        file.seek(backup_entries * SECTOR)
        file.write(entries)
        file.seek(backup_lba * SECTOR)
        file.write(gpt_header(backup_lba, 1, backup_entries, entries_crc, last_usable))


def mtools_env() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "LC_ALL": "C",
            "TZ": "UTC",
            "SOURCE_DATE_EPOCH": str(EPOCH),
            "MTOOLSRC": "/dev/null",
            "MTOOLS_SKIP_CHECK": "0",
        }
    )
    return environment


class Fixture:
    def __init__(self, root: Path):
        self.root = root
        self.raw = root / "published.raw"
        self.zst = root / "published.raw.zst"
        self.uki = root / UKI_NAME
        self.adapter = root / "duranium-lvm-adapter.cpio"
        self.esp = root / "esp.img"
        self.uki.write_bytes(make_uki())
        self.adapter.write_bytes(make_adapter())
        with self.esp.open("wb") as file:
            file.truncate(ESP_BYTES)
        environment = mtools_env()
        subprocess.run(
            ["mformat", "-i", str(self.esp), "-F", "-v", "ESP", "::"],
            env=environment,
            check=True,
            capture_output=True,
        )
        for directory in ("::/EFI", "::/EFI/Linux", "::/loader", "::/loader/entries"):
            subprocess.run(
                ["mmd", "-i", str(self.esp), directory],
                env=environment,
                check=True,
                capture_output=True,
            )
        loader = root / "original-loader.conf"
        loader.write_text("#timeout 3\n#console-mode keep\n")
        for path in (self.uki, loader):
            os.utime(path, (EPOCH, EPOCH))
        subprocess.run(
            ["mcopy", "-m", "-i", str(self.esp), str(self.uki), f"::/EFI/Linux/{UKI_NAME}"],
            env=environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["mcopy", "-m", "-i", str(self.esp), str(loader), "::/loader/loader.conf"],
            env=environment,
            check=True,
            capture_output=True,
        )
        with self.raw.open("wb") as file:
            file.truncate(DISK_BYTES)
        write_gpt(self.raw)
        with self.raw.open("r+b", buffering=0) as output, self.esp.open("rb", buffering=0) as esp:
            output.seek(ESP_START * SECTOR)
            while block := esp.read(1024 * 1024):
                output.write(block)
        subprocess.run(
            ["zstd", "-q", "-f", str(self.raw), "-o", str(self.zst)],
            check=True,
            capture_output=True,
        )

    def command(
        self,
        output: Path,
        provenance: Path,
        source_format: str = "raw",
        overrides: dict[str, str] | None = None,
    ) -> list[str]:
        source = self.raw if source_format == "raw" else self.zst
        values = {
            "source": str(source),
            "source-format": source_format,
            "source-sha256": digest(source),
            "raw-sha256": digest(self.raw),
            "disk-bytes": str(DISK_BYTES),
            "disk-guid": str(DISK_GUID),
            "esp-partuuid": str(ESP_UUID),
            "esp-start-lba": str(ESP_START),
            "esp-sectors": str(ESP_SECTORS),
            "uki": str(self.uki),
            "uki-sha256": digest(self.uki),
            "cmdline-sha256": hashlib.sha256(CMDLINE.encode()).hexdigest(),
            "adapter": str(self.adapter),
            "adapter-sha256": digest(self.adapter),
            "output": str(output),
            "provenance": str(provenance),
        }
        values.update(overrides or {})
        command = [str(TOOL)]
        for option, value in values.items():
            command.extend([f"--{option}", value])
        return command


class PrepareDuraniumDiskTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for executable in ("mcopy", "mdir", "mformat", "mmd", "zstd"):
            if shutil.which(executable) is None:
                raise unittest.SkipTest(f"missing test dependency: {executable}")

    def run_tool(self, command: list[str], success: bool = True):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if success and result.returncode != 0:
            self.fail(f"tool failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail("tool unexpectedly succeeded")
        return result

    def test_bls_cmdline_adds_or_preserves_exactly_one_sysrq_policy(self):
        render_bls = TOOL_MODULE["render_bls"]
        plain = render_bls("test.efi", "quiet").decode()
        self.assertEqual(plain.count("sysrq_always_enabled=1"), 1)
        self.assertIn(
            "options quiet root=dissect mount.usr=dissect sysrq_always_enabled=1\n",
            plain,
        )

        embedded = render_bls("test.efi", "quiet sysrq_always_enabled=1").decode()
        self.assertEqual(embedded.count("sysrq_always_enabled=1"), 1)
        self.assertIn(
            "options quiet sysrq_always_enabled=1 root=dissect mount.usr=dissect\n",
            embedded,
        )

        with self.assertRaisesRegex(
            TOOL_MODULE["ToolError"], "duplicate sysrq_always_enabled=1"
        ):
            render_bls(
                "test.efi",
                "quiet sysrq_always_enabled=1 sysrq_always_enabled=1",
            )

    def extract(self, disk: Path, esp_path: str, destination: Path) -> bytes:
        subprocess.run(
            [
                "mcopy",
                "-i",
                f"{disk}@@{ESP_START * SECTOR}",
                esp_path.replace("/", "::/", 1),
                str(destination),
            ],
            env=mtools_env(),
            check=True,
            capture_output=True,
        )
        return destination.read_bytes()

    def test_raw_and_zstd_derivations_are_identical_and_verified(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            original_hash = digest(fixture.raw)
            outputs = []
            for index, source_format in enumerate(("raw", "raw", "zst")):
                output = root / f"derived-{index}.raw"
                provenance = root / f"derived-{index}.json"
                result = self.run_tool(fixture.command(output, provenance, source_format))
                emitted = json.loads(result.stdout)
                recorded = json.loads(provenance.read_text())
                self.assertEqual(emitted, recorded)
                self.assertEqual(recorded["format"], "frankensargo.duranium-derived-disk.v1")
                self.assertEqual(recorded["output"]["sha256"], digest(output))
                self.assertEqual(recorded["output"]["bytes"], DISK_BYTES)
                self.assertEqual(recorded["disk"]["disk_guid"], str(DISK_GUID))
                self.assertEqual(recorded["disk"]["esp"]["partuuid"], str(ESP_UUID))
                self.assertIn(
                    "loader/loader.conf default selection support",
                    recorded["requirements"]["pocketboot"],
                )
                self.assertEqual(
                    recorded["requirements"]["duranium_cmdline"],
                    ["root=dissect", "mount.usr=dissect", "sysrq_always_enabled=1"],
                )
                outputs.append(output)
            self.assertEqual(digest(fixture.raw), original_hash)
            self.assertEqual(digest(outputs[0]), digest(outputs[1]))
            self.assertEqual(digest(outputs[0]), digest(outputs[2]))
            self.assertNotEqual(digest(outputs[0]), original_hash)

            adapter = self.extract(
                outputs[0],
                "/EFI/Linux/frankensargo-duranium-lvm-26070701.cpio",
                root / "extracted-adapter",
            )
            self.assertEqual(adapter, fixture.adapter.read_bytes())
            bls = self.extract(
                outputs[0],
                "/loader/entries/frankensargo-duranium.conf",
                root / "extracted-bls",
            ).decode()
            self.assertIn(f"uki /EFI/Linux/{UKI_NAME}\n", bls)
            self.assertIn("profile 0\n", bls)
            self.assertIn("title Duranium 26070701 (frankensargo LVM)\n", bls)
            self.assertIn("version 26070701\n", bls)
            self.assertIn("initrd /EFI/Linux/frankensargo-duranium-lvm-26070701.cpio\n", bls)
            self.assertIn(f"options {CMDLINE} root=dissect mount.usr=dissect sysrq_always_enabled=1\n", bls)
            loader = self.extract(
                outputs[0], "/loader/loader.conf", root / "extracted-loader"
            )
            self.assertEqual(
                loader,
                b"#timeout 3\n#console-mode keep\ndefault frankensargo-duranium.conf\n",
            )
            uki = self.extract(
                outputs[0], f"/EFI/Linux/{UKI_NAME}", root / "extracted-uki"
            )
            self.assertEqual(uki, fixture.uki.read_bytes())

    def test_identity_and_geometry_failures_publish_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            cases = (
                {"source-sha256": "0" * 64},
                {"raw-sha256": "0" * 64},
                {"cmdline-sha256": "0" * 64},
                {"esp-start-lba": str(ESP_START + 1)},
                {"disk-guid": "11111111-2222-3333-4444-555555555555"},
            )
            for index, overrides in enumerate(cases):
                output = root / f"failed-{index}.raw"
                provenance = root / f"failed-{index}.json"
                self.run_tool(
                    fixture.command(output, provenance, overrides=overrides), success=False
                )
                self.assertFalse(output.exists())
                self.assertFalse(provenance.exists())

    def test_rejects_symlink_input_and_existing_output_without_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            linked_uki = root / "linked.efi"
            linked_uki.symlink_to(fixture.uki)
            output = root / "linked-output.raw"
            provenance = root / "linked-output.json"
            result = self.run_tool(
                fixture.command(
                    output,
                    provenance,
                    overrides={"uki": str(linked_uki), "uki-sha256": digest(fixture.uki)},
                ),
                success=False,
            )
            self.assertIn("without following a final symlink", result.stderr)
            self.assertFalse(output.exists())

            existing = root / "existing.raw"
            existing.write_bytes(b"do-not-replace")
            result = self.run_tool(
                fixture.command(existing, root / "existing.json"), success=False
            )
            self.assertIn("exists; refusing to replace", result.stderr)
            self.assertEqual(existing.read_bytes(), b"do-not-replace")

    def test_corrupt_gpt_and_fat_are_rejected_even_when_artifact_hashes_match(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            corruptions = (
                ("gpt", SECTOR + 16, b"\0\0\0\0", "GPT header CRC mismatch"),
                ("fat", ESP_START * SECTOR + 11, b"\0\4", "FAT sector size"),
            )
            for name, offset, replacement, expected_error in corruptions:
                source = root / f"corrupt-{name}.raw"
                shutil.copyfile(fixture.raw, source)
                with source.open("r+b", buffering=0) as file:
                    file.seek(offset)
                    file.write(replacement)
                source_sha = digest(source)
                output = root / f"corrupt-{name}-derived.raw"
                provenance = root / f"corrupt-{name}-derived.json"
                result = self.run_tool(
                    fixture.command(
                        output,
                        provenance,
                        overrides={
                            "source": str(source),
                            "source-sha256": source_sha,
                            "raw-sha256": source_sha,
                        },
                    ),
                    success=False,
                )
                self.assertIn(expected_error, result.stderr)
                self.assertFalse(output.exists())
                self.assertFalse(provenance.exists())


if __name__ == "__main__":
    unittest.main()
