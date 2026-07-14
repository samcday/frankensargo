#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "audit-duranium-import"
PARTUUID = "db04e713-11c3-4d68-bec2-8cc483bd3891"
PV_UUID = "AAAAAA-bbbb-cccc-dddd-eeee-ffff-GGGGGG"
VG_UUID = "BBBBBB-1111-2222-3333-4444-5555-CCCCCC"
LV_UUID = "CCCCCC-aaaa-bbbb-cccc-dddd-eeee-FFFFFF"
REQUIRED_CMDLINE = [
    "root=dissect",
    "mount.usr=dissect",
    "sysrq_always_enabled=1",
    "usrhash=<sha256>",
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def append_newc(output: bytearray, name: str, mode: int, data: bytes, inode: int) -> None:
    encoded = name.encode() + b"\0"
    fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0, 0, 0, len(encoded), 0)
    output.extend(b"070701" + b"".join(f"{field:08x}".encode() for field in fields))
    output.extend(encoded)
    output.extend(b"\0" * (-len(output) % 4))
    output.extend(data)
    output.extend(b"\0" * (-len(output) % 4))


def make_adapter(required_cmdline: list[str] | None = None) -> tuple[bytes, bytes]:
    manifest = (
        json.dumps(
            {
                "disk_lv": {
                    "name": "disk-duranium",
                    "required_tag": "pocketboot.disk.v1",
                    "uuid": LV_UUID,
                },
                "format": "frankensargo.duranium-lvm-adapter.v1",
                "pv_uuid": PV_UUID,
                "required_cmdline": required_cmdline or REQUIRED_CMDLINE,
                "userdata_partuuid": PARTUUID,
                "vg_uuid": VG_UUID,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()
    output = bytearray()
    append_newc(output, "init", stat.S_IFREG | 0o755, b"#!/bin/sh\n", 1)
    append_newc(
        output,
        "usr/lib/frankensargo-duranium/build-manifest.json",
        stat.S_IFREG | 0o644,
        manifest,
        2,
    )
    append_newc(output, "TRAILER!!!", 0, b"", 3)
    output.extend(b"\0" * (-len(output) % 512))
    return bytes(output), manifest


class Fixture:
    def __init__(self, root: Path, *, cmdline: list[str] | None = None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.disk = root / "derived.raw"
        self.provenance = root / "derived.json"
        self.adapter = root / "adapter.cpio"
        self.disk_data = b"A" + b"\0" * 1023
        self.disk.write_bytes(self.disk_data)
        adapter, manifest = make_adapter(cmdline)
        self.adapter.write_bytes(adapter)
        self.provenance_data = {
            "format": "frankensargo.duranium-derived-disk.v1",
            "inputs": {
                "adapter": {
                    "basename": self.adapter.name,
                    "bytes": len(adapter),
                    "sha256": digest(adapter),
                    "format": "frankensargo.duranium-lvm-adapter.v1",
                    "build_manifest_sha256": digest(manifest),
                }
            },
            "output": {
                "basename": self.disk.name,
                "bytes": len(self.disk_data),
                "sha256": digest(self.disk_data),
            },
            "requirements": {
                "duranium_cmdline": REQUIRED_CMDLINE[:3],
            },
        }
        self.write_provenance()

    def write_provenance(self) -> None:
        self.provenance.write_text(json.dumps(self.provenance_data) + "\n")

    def command(self, overrides: list[str] | None = None) -> list[str]:
        return [
            str(TOOL),
            "--disk",
            str(self.disk),
            "--provenance",
            str(self.provenance),
            "--adapter",
            str(self.adapter),
            "--userdata-partuuid",
            PARTUUID,
            "--pv-uuid",
            PV_UUID,
            "--vg-uuid",
            VG_UUID,
            "--disk-lv-uuid",
            LV_UUID,
            "--virtual-bytes",
            "2048",
            "--pool-bytes",
            "2048",
            "--chunk-bytes",
            "512",
            "--minimum-pool-free-bytes",
            "1024",
            *(overrides or []),
        ]


class AuditDuraniumImportTests(unittest.TestCase):
    def run_tool(self, command: list[str], success: bool = True):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if success and result.returncode != 0:
            self.fail(f"tool failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail("tool unexpectedly succeeded")
        return result

    def test_exact_binding_sparse_ceiling_and_full_readback_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Fixture(Path(temporary))
            result = self.run_tool(fixture.command())
            audit = json.loads(result.stdout)
            self.assertEqual(audit["schema"], "org.frankensargo.duranium-import-audit/1")
            self.assertEqual(audit["binding"]["disk_lv_uuid"], LV_UUID)
            self.assertEqual(audit["thin_pool_gate"]["source_chunks"], 2)
            self.assertEqual(audit["thin_pool_gate"]["source_nonzero_chunks"], 1)
            self.assertEqual(
                audit["thin_pool_gate"]["new_mapped_bytes_upper_bound"], 512
            )
            self.assertEqual(audit["thin_pool_gate"]["sparse_write_extent_count"], 1)
            self.assertEqual(
                audit["thin_pool_gate"]["maximum_pool_metadata_percent"], "75.00"
            )
            self.assertEqual(
                audit["import_contract"]["write_extents"],
                [
                    {
                        "chunk_count": 1,
                        "destination_offset_bytes": 0,
                        "sha256": digest(b"A" + b"\0" * 511),
                        "source_bytes": 512,
                        "source_offset_bytes": 0,
                        "start_chunk": 0,
                    }
                ],
            )
            self.assertEqual(
                audit["destination"]["full_lv_sha256"],
                digest(fixture.disk_data + b"\0" * 1024),
            )

    def test_rejects_wrong_binding_hash_and_noncanonical_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            result = self.run_tool(
                fixture.command(["--disk-lv-uuid", PV_UUID]), success=False
            )
            self.assertIn("disk-LV binding is not exact", result.stderr)

            fixture.provenance_data["output"]["sha256"] = "0" * 64
            fixture.write_provenance()
            result = self.run_tool(fixture.command(), success=False)
            self.assertIn("derived-disk SHA-256 mismatch", result.stderr)

            duplicate = Fixture(
                root / "duplicate",
                cmdline=REQUIRED_CMDLINE[:3]
                + ["sysrq_always_enabled=1", "usrhash=<sha256>"],
            )
            result = self.run_tool(duplicate.command(), success=False)
            self.assertIn("required_cmdline is not the canonical exact list", result.stderr)

    def test_rejects_pool_budget_and_symlink_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = Fixture(root)
            fixture.disk.write_bytes(b"A" * 1024)
            fixture.disk_data = b"A" * 1024
            fixture.provenance_data["output"]["sha256"] = digest(fixture.disk_data)
            fixture.write_provenance()
            result = self.run_tool(
                fixture.command(["--minimum-pool-free-bytes", "1536"]),
                success=False,
            )
            self.assertIn("could exceed the pool-data budget", result.stderr)

            result = self.run_tool(
                fixture.command(["--maximum-pool-metadata-percent", "99"]),
                success=False,
            )
            self.assertIn("no greater than 75.00", result.stderr)

            linked = root / "linked.raw"
            linked.symlink_to(fixture.disk)
            result = self.run_tool(
                fixture.command(["--disk", str(linked)]), success=False
            )
            self.assertIn("without following a final symlink", result.stderr)


if __name__ == "__main__":
    unittest.main()
