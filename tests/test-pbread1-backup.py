#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import pbread1  # noqa: E402


SERIAL = "TEST-SARGO"
PARTUUID = "db04e713-11c3-4d68-bec2-8cc483bd3891"
PARTTYPE = "1b81e7e6-f50d-419b-a739-2aeef8da3335"
DISK_GUID = "00000000-0000-0000-0000-000000000000"
SECTOR_BYTES = 512
SECTORS = 20
RAW_BYTES = SECTORS * SECTOR_BYTES


def write_inventory(path: Path) -> None:
    inventory: dict[str, object] = {
        "schema": "org.frankensargo.inventory/1",
        "device": {
            "adb_serial": SERIAL,
            "adb_state": "recovery",
            "compatible": ["google,sargo", "qcom,sdm670"],
            "emmc": {
                "cid": "13014e53304a394b381011182ce76600",
                "logical_sector_size": SECTOR_BYTES,
                "sector_count": "122142720",
                "size_bytes": str(122142720 * SECTOR_BYTES),
            },
            "kernel_release": "test",
            "model": "Pixel 3a",
            "product": "sargo",
        },
        "gpt": {
            "backup_entry_array_independent": False,
            "backup_entry_array_layout": "aliases-primary",
            "backup_header": {},
            "disk_guid": DISK_GUID,
            "disk_guid_is_zero": True,
            "entry_array_crc32": "0x00000000",
            "entry_array_sha256": "sha256:" + "11" * 32,
            "entry_count": 128,
            "entry_size": 128,
            "first_usable_lba": "34",
            "last_usable_lba": "122142686",
            "partitions": [
                {
                    "attributes": "0x0000000000000000",
                    "byte_size": str(RAW_BYTES),
                    "kernel_node_observation": "mmcblk0p72",
                    "last_lba": str(100 + SECTORS - 1),
                    "name": "userdata",
                    "number": 72,
                    "partuuid": PARTUUID,
                    "sector_count": str(SECTORS),
                    "start_lba": "100",
                    "type_guid": PARTTYPE,
                }
            ],
            "primary_header": {},
        },
    }
    inventory["canonical_sha256"] = "sha256:" + pbread1.sha256_bytes(pbread1.canonical_json_bytes(inventory))
    path.write_bytes(pbread1.pretty_json_bytes(inventory))


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="frankensargo-pbread1-")
        self.root = Path(self.temporary.name)
        self.inventory = self.root / "inventory.json"
        self.image = self.root / "pocketboot.img"
        self.source = self.root / "userdata.raw"
        write_inventory(self.inventory)
        self.image.write_bytes(b"test pocketboot image\n")
        self.source.write_bytes(bytes(range(256)) * (RAW_BYTES // 256))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def manifest(self, chunk_bytes: int = 4096) -> dict[str, object]:
        return pbread1.manifest_from_inputs(
            self.inventory,
            SERIAL,
            PARTUUID,
            "userdata",
            self.image,
            chunk_bytes,
            run_uuid="00000000-0000-4000-8000-000000000001",
            created_at="2026-07-12T00:00:00Z",
        )


class PlanningTests(Fixture):
    def test_exact_frankensargo_chunk_plan(self) -> None:
        plan = pbread1.plan_chunks(53_648_801_280, pbread1.DEFAULT_CHUNK_BYTES)
        self.assertEqual(len(plan), 800)
        self.assertEqual(plan[0], (0, 0x04000000))
        self.assertEqual(plan[-1], (0xC7C000000, 0x01B7BE00))
        final_command = (
            f"oem read {PARTUUID.replace('-', '')} "
            f"{plan[-1][0]:x} {plan[-1][1]:x}"
        )
        self.assertEqual(len(final_command), 59)

    def test_manifest_binds_inventory_and_partition(self) -> None:
        manifest = self.manifest()
        self.assertEqual(manifest["device"]["fastboot_serial"], SERIAL)
        self.assertEqual(manifest["device"]["emmc_cid"], "13014e53304a394b381011182ce76600")
        self.assertEqual(manifest["partition"]["partuuid"], PARTUUID)
        self.assertEqual(manifest["partition"]["raw_bytes"], str(RAW_BYTES))
        self.assertEqual(manifest["transport"]["chunk_count"], 3)

    def test_changed_inventory_hash_is_rejected(self) -> None:
        inventory = json.loads(self.inventory.read_text())
        inventory["device"]["model"] = "tampered"
        self.inventory.write_text(json.dumps(inventory))
        with self.assertRaisesRegex(pbread1.BackupError, "canonical hash mismatch"):
            self.manifest()

    def test_dry_run_writes_nothing(self) -> None:
        run_dir = self.root / "dry-run"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = pbread1.main(
                [
                    "backup",
                    "--run-dir",
                    str(run_dir),
                    "--inventory",
                    str(self.inventory),
                    "--serial",
                    SERIAL,
                    "--partuuid",
                    PARTUUID,
                    "--pocketboot-image",
                    str(self.image),
                    "--dry-run",
                ]
            )
        self.assertEqual(result, 0)
        self.assertFalse(run_dir.exists())
        self.assertIn("no fastboot command executed", stderr.getvalue())


class EnvelopeTests(Fixture):
    def stage(self, path: Path, offset: int = 0, length: int = 4096) -> pbread1.PartitionBinding:
        manifest = self.manifest()
        binding = pbread1.PartitionBinding.from_manifest(manifest)
        pbread1.OfflineTransport(self.source).stage_range(binding, offset, length, path)
        return binding

    def test_range_envelope_round_trip(self) -> None:
        envelope = self.root / "range.pbr"
        output = self.root / "chunk.tmp"
        binding = self.stage(envelope)
        digest = pbread1.extract_envelope(envelope, output, binding, 0, 4096)
        self.assertEqual(output.read_bytes(), self.source.read_bytes()[:4096])
        self.assertEqual(digest, pbread1.sha256_file(output))

    def test_hash_only_record_round_trip(self) -> None:
        manifest = self.manifest()
        binding = pbread1.PartitionBinding.from_manifest(manifest)
        record = self.root / "hash.pbr"
        pbread1.OfflineTransport(self.source).stage_hash(binding, record)
        digest = pbread1.extract_envelope(
            record,
            None,
            binding,
            0,
            RAW_BYTES,
            flags=pbread1.FLAG_HASH_ONLY,
        )
        self.assertEqual(record.stat().st_size, pbread1.HEADER_BYTES)
        self.assertEqual(digest, pbread1.sha256_file(self.source))

    def test_corrupt_envelopes_fail_closed(self) -> None:
        original = self.root / "original.pbr"
        binding = self.stage(original)
        cases: dict[str, bytes] = {}
        raw = original.read_bytes()
        cases["magic"] = b"X" + raw[1:]
        cases["payload"] = raw[:-1] + bytes([raw[-1] ^ 1])
        cases["truncated"] = raw[:-1]
        cases["trailing"] = raw + b"X"
        reserved = bytearray(raw)
        reserved[0x108] = 1
        cases["reserved"] = bytes(reserved)
        for name, contents in cases.items():
            with self.subTest(name=name):
                path = self.root / f"{name}.pbr"
                output = self.root / f"{name}.tmp"
                path.write_bytes(contents)
                with self.assertRaises(pbread1.BackupError):
                    pbread1.extract_envelope(path, output, binding, 0, 4096)
                self.assertFalse(output.exists())

    def test_wrong_requested_range_is_rejected(self) -> None:
        envelope = self.root / "range.pbr"
        binding = self.stage(envelope)
        with self.assertRaisesRegex(pbread1.BackupError, "source_offset mismatch"):
            pbread1.extract_envelope(envelope, None, binding, 512, 4096)


class ResumeTests(Fixture):
    def test_cli_offline_backup_and_verify(self) -> None:
        run_dir = self.root / "cli-run"
        arguments = [
            "backup",
            "--run-dir",
            str(run_dir),
            "--inventory",
            str(self.inventory),
            "--serial",
            SERIAL,
            "--partuuid",
            PARTUUID,
            "--pocketboot-image",
            str(self.image),
            "--offline-source",
            str(self.source),
            "--chunk-bytes",
            "4096",
        ]
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pbread1.main(arguments), 0)
            self.assertEqual(pbread1.main(["verify", "--run-dir", str(run_dir)]), 0)
        self.assertEqual((run_dir / "userdata.raw").read_bytes(), self.source.read_bytes())

    def test_offline_backup_resume_assembly_and_verification(self) -> None:
        run_dir = self.root / "run"
        manifest = self.manifest()
        first = pbread1.execute_backup(run_dir, manifest, pbread1.OfflineTransport(self.source))
        self.assertEqual((first.downloaded_chunks, first.skipped_chunks), (3, 0))
        self.assertEqual((run_dir / "userdata.raw").read_bytes(), self.source.read_bytes())
        self.assertEqual(pbread1.verify_run(run_dir), pbread1.sha256_file(self.source))

        resumed_manifest = self.manifest()
        resumed = pbread1.execute_backup(run_dir, resumed_manifest, pbread1.OfflineTransport(self.source))
        self.assertEqual((resumed.downloaded_chunks, resumed.skipped_chunks), (0, 3))
        self.assertEqual(pbread1.verify_run(run_dir), first.raw_sha256)

    def test_backup_tree_is_private_and_insecure_run_or_lock_is_rejected(self) -> None:
        run_dir = self.root / "private-run"
        pbread1.execute_backup(run_dir, self.manifest(), pbread1.OfflineTransport(self.source))
        self.assertEqual(stat.S_IMODE(run_dir.lstat().st_mode), 0o700)
        for path in run_dir.rglob("*"):
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(stat.S_IMODE(path.lstat().st_mode), expected, path)

        insecure = self.root / "insecure-run"
        insecure.mkdir(mode=0o755)
        with self.assertRaisesRegex(pbread1.BackupError, "mode 0700"):
            pbread1.execute_backup(
                insecure, self.manifest(), pbread1.OfflineTransport(self.source)
            )

        symlinked = self.root / "symlinked-lock"
        symlinked.mkdir(mode=0o700)
        (symlinked / ".lock").symlink_to(self.source)
        with self.assertRaisesRegex(pbread1.BackupError, "cannot open backup run lock"):
            with pbread1.run_lock(symlinked, exclusive=False):
                self.fail("symlinked PBREAD lock was accepted")

    def test_corrupt_chunk_is_quarantined_and_recaptured(self) -> None:
        run_dir = self.root / "run"
        manifest = self.manifest()
        pbread1.execute_backup(run_dir, manifest, pbread1.OfflineTransport(self.source))
        damaged = pbread1.chunk_path(run_dir, 1)
        contents = bytearray(damaged.read_bytes())
        contents[0] ^= 1
        damaged.write_bytes(contents)

        repaired = pbread1.execute_backup(run_dir, self.manifest(), pbread1.OfflineTransport(self.source))
        self.assertEqual((repaired.downloaded_chunks, repaired.skipped_chunks), (1, 2))
        self.assertTrue(any((run_dir / "rejected").iterdir()))
        self.assertEqual(pbread1.verify_run(run_dir), pbread1.sha256_file(self.source))

    def test_manifest_tampering_stops_resume(self) -> None:
        run_dir = self.root / "run"
        pbread1.execute_backup(run_dir, self.manifest(), pbread1.OfflineTransport(self.source))
        manifest_path = run_dir / "manifest.json"
        manifest_path.write_bytes(manifest_path.read_bytes() + b" ")
        with self.assertRaisesRegex(pbread1.BackupError, "checksum mismatch"):
            pbread1.verify_run(run_dir)

    def test_full_source_hash_must_match_assembled_image(self) -> None:
        class MismatchingSource(pbread1.OfflineTransport):
            def stage_hash(self, binding: pbread1.PartitionBinding, destination: Path) -> None:
                pbread1.atomic_write(
                    destination,
                    pbread1.header_for_hash(binding, "00" * 32).encode(),
                )

        run_dir = self.root / "mismatch-run"
        with self.assertRaisesRegex(pbread1.BackupError, "does not match assembled backup"):
            pbread1.execute_backup(run_dir, self.manifest(), MismatchingSource(self.source))
        with self.assertRaisesRegex(pbread1.BackupError, "source verification is not matched"):
            pbread1.verify_run(run_dir)


class FastbootCommandTests(Fixture):
    def test_every_fastboot_command_is_exact_serial_scoped(self) -> None:
        executable = self.root / "fastboot-test"
        executable.write_text("#!/bin/sh\nexit 99\n")
        executable.chmod(0o755)
        calls: list[list[str]] = []

        def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "ok\n")

        transport = pbread1.FastbootTransport(str(executable), SERIAL, runner=runner)
        transport._run(["oem", "read", PARTUUID.replace("-", ""), "0", "1000"])
        self.assertEqual(
            calls,
            [[str(executable), "-s", SERIAL, "oem", "read", PARTUUID.replace("-", ""), "0", "1000"]],
        )


if __name__ == "__main__":
    unittest.main()
