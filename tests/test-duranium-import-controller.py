#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import duranium_import as importer  # noqa: E402


PARTUUID = "db04e713-11c3-4d68-bec2-8cc483bd3891"
PV_UUID = "AAAAAA-bbbb-cccc-dddd-eeee-ffff-GGGGGG"
VG_UUID = "BBBBBB-1111-2222-3333-4444-5555-CCCCCC"
POOL_UUID = "CCCCCC-1111-2222-3333-4444-5555-DDDDDD"
DATA_UUID = "DDDDDD-1111-2222-3333-4444-5555-EEEEEE"
META_UUID = "EEEEEE-1111-2222-3333-4444-5555-FFFFFF"
DISK_UUID = "FFFFFF-1111-2222-3333-4444-5555-AAAAAA"
SERIAL = "99NAY1AZG1"
VIRTUAL_BYTES = 20 * 1024 * 1024 * 1024
CHUNK = 512


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def binding() -> importer.Binding:
    publish = (
        "/sbin/lvm.static", "lvchange", "--devices", "/dev/mmcblk0p72", "--nohints",
        "--config", importer.REPORT_CONFIG, "--permission", "r", "--deltag",
        "greygoo.import-pending", "--addtag", "pocketboot.disk.v1", "franken/disk-duranium",
    )
    return importer.Binding(
        serial=SERIAL,
        partuuid=PARTUUID,
        anchor="/dev/mmcblk0p72",
        pv_uuid=PV_UUID,
        vg_uuid=VG_UUID,
        pool_uuid=POOL_UUID,
        pool_data_uuid=DATA_UUID,
        pool_metadata_uuid=META_UUID,
        disk_uuid=DISK_UUID,
        disk_bytes=VIRTUAL_BYTES,
        pool_bytes=VIRTUAL_BYTES,
        chunk_bytes=CHUNK,
        minimum_free_bytes=16 * 1024 * 1024 * 1024,
        operation_uuid="01234567-89ab-4cde-8f01-23456789abcd",
        authorization_sha256="sha256:" + "1" * 64,
        plan_sha256="2" * 64,
        checkpoint_sha256="3" * 64,
        checkpoint_state_sha256="sha256:" + "4" * 64,
        all_lvm_uuids=tuple(sorted((PV_UUID, VG_UUID, POOL_UUID, DATA_UUID, META_UUID, DISK_UUID))),
        publish_argv=publish,
    )


class Inputs:
    def __init__(self, root: Path, *, two_extents: bool = False):
        root.mkdir(parents=True, exist_ok=True)
        self.disk_path = root / "derived.raw"
        self.provenance_path = root / "derived.json"
        self.adapter_path = root / "adapter.cpio"
        self.tool_path = root / "audit-tool.py"
        self.pocketboot_image_path = root / "pocketboot.img"
        self.pocketboot_provenance_path = root / "pocketboot.provenance.json"
        disk_data = b"A" + b"\0" * 511
        if two_extents:
            disk_data += b"\0" * 512 + b"B" + b"\0" * 511
        self.disk_path.write_bytes(disk_data)
        self.provenance_path.write_bytes(b'{"provenance":true}\n')
        self.adapter_path.write_bytes(b"adapter\n")
        self.tool_path.write_bytes(b"#!/usr/bin/env python3\n")
        self.pocketboot_image_path.write_bytes(b"synthetic pocketboot image")
        self.pocketboot_provenance_path.write_bytes(b'{"synthetic":true}\n')
        self.files = [
            importer.HeldFile.open("derived disk", self.disk_path),
            importer.HeldFile.open("derived provenance", self.provenance_path),
            importer.HeldFile.open("adapter", self.adapter_path),
            importer.HeldFile.open("audit tool", self.tool_path),
            importer.HeldFile.open("PocketBoot image", self.pocketboot_image_path),
            importer.HeldFile.open("PocketBoot provenance", self.pocketboot_provenance_path),
        ]
        (
            self.disk,
            self.provenance,
            self.adapter,
            self.tool,
            self.pocketboot_image,
            self.pocketboot_provenance,
        ) = self.files
        extents = [
            {
                "start_chunk": 0,
                "chunk_count": 1,
                "source_offset_bytes": 0,
                "source_bytes": 512,
                "destination_offset_bytes": 0,
                "sha256": digest(disk_data[:512]),
            }
        ]
        if two_extents:
            extents.append(
                {
                    "start_chunk": 2,
                    "chunk_count": 1,
                    "source_offset_bytes": 1024,
                    "source_bytes": 512,
                    "destination_offset_bytes": 1024,
                    "sha256": digest(disk_data[1024:1536]),
                }
            )
        self.audit = {
            "schema": importer.AUDIT_SCHEMA,
            "binding": {
                "userdata_partuuid": PARTUUID,
                "pv_uuid": PV_UUID,
                "vg_uuid": VG_UUID,
                "disk_lv_name": "disk-duranium",
                "disk_lv_uuid": DISK_UUID,
                "required_import_tag": "greygoo.import-pending",
                "published_tag": "pocketboot.disk.v1",
            },
            "inputs": {
                "disk": {"basename": self.disk_path.name, "bytes": len(disk_data), "sha256": self.disk.sha256},
                "provenance": {"basename": self.provenance_path.name, "sha256": self.provenance.sha256},
                "adapter": {"basename": self.adapter_path.name, "bytes": self.adapter.size, "sha256": self.adapter.sha256},
            },
            "destination": {
                "virtual_bytes": VIRTUAL_BYTES,
                "zero_tail_bytes": VIRTUAL_BYTES - len(disk_data),
                "full_lv_sha256": "9" * 64,
            },
            "thin_pool_gate": {
                "pool_bytes": VIRTUAL_BYTES,
                "chunk_bytes": CHUNK,
                "new_mapped_bytes_upper_bound": len(extents) * CHUNK,
                "required_minimum_pool_free_bytes": 16 * 1024 * 1024 * 1024,
                "maximum_pool_metadata_percent": "75.00",
                "sparse_write_extent_count": len(extents),
                "requires_live_pre_and_post_pool_usage_check": True,
                "requires_live_before_every_remaining_extent_check": True,
                "requires_live_full_hash_and_publication_check": True,
            },
            "import_contract": {
                "write_block_bytes": CHUNK,
                "maximum_write_bytes": 64 * 1024 * 1024,
                "skip_all_zero_blocks": True,
                "preserve_unwritten_zero_tail": True,
                "readback_bytes": VIRTUAL_BYTES,
                "readback_sha256": "9" * 64,
                "write_extents": extents,
            },
        }
        self.contract = importer.parse_audit(
            self.audit, importer.sha256_bytes(importer.canonical_bytes(self.audit))
        )

    def close(self) -> None:
        for item in reversed(self.files):
            item.close()


def live_state(bound: importer.Binding) -> importer.LiveState:
    return importer.LiveState(
        serial=bound.serial,
        partuuid=bound.partuuid,
        anchor=bound.anchor,
        pv_uuid=bound.pv_uuid,
        vg_uuid=bound.vg_uuid,
        pool_uuid=bound.pool_uuid,
        pool_data_uuid=bound.pool_data_uuid,
        pool_metadata_uuid=bound.pool_metadata_uuid,
        pool_bytes=bound.pool_bytes,
        pool_chunk_bytes=bound.chunk_bytes,
        pool_segtype="thin-pool",
        pool_data_percent="0.00",
        pool_metadata_percent="0.00",
        pool_healthy=True,
        pool_discards="nopassdown",
        pool_when_full="error",
        disk_uuid=bound.disk_uuid,
        disk_bytes=bound.disk_bytes,
        disk_segtype="thin",
        disk_pool_uuid=bound.pool_uuid,
        disk_tags=importer.REQUIRED_PENDING_TAGS,
        disk_permission="rw",
        disk_active=False,
        disk_dm_uuid=None,
        disk_sectors=None,
        disk_kernel_ro=None,
        disk_quiescent=True,
    )


class FakeRemote:
    def __init__(self, bound: importer.Binding):
        self.binding = bound
        self.state = live_state(bound)
        self.calls: list[str] = []
        self.writes: list[int] = []
        self.fail_extent_once: int | None = None
        self.fail_phase_once: str | None = None
        self.disconnect_phase_once: str | None = None
        self.wrong_argv_phase_once: str | None = None
        self.completed_extents: set[int] = set()
        self.corrupt_extents: set[int] = set()
        self.drift_after_extent: tuple[int, str, str] | None = None
        self.bad_attestation = False
        self.drift_after_sync: str | None = None
        self.legacy = False
        self.disconnect_after_attestation_once = False
        self.disconnect_after_publication_once = False
        self._attestation_completed = False

    def result(
        self,
        argv,
        rc: int = 0,
        *,
        stdout: bytes = b"",
        stderr: bytes | None = None,
    ) -> importer.RemoteResult:
        if stderr is None:
            stderr = b"synthetic" if rc else b""
        return importer.RemoteResult(tuple(argv), rc, stdout, stderr, not self.legacy)

    def phase_result(
        self,
        name: str,
        argv,
        *,
        stdout: bytes = b"",
        stderr: bytes | None = None,
    ) -> importer.RemoteResult:
        if self.disconnect_phase_once == name:
            self.disconnect_phase_once = None
            raise importer.ImportFailure(f"synthetic disconnect after {name}")
        rc = 0
        if self.fail_phase_once == name:
            self.fail_phase_once = None
            rc = 5
        if self.wrong_argv_phase_once == name:
            self.wrong_argv_phase_once = None
            argv = ("/bin/true",)
        return self.result(argv, rc, stdout=stdout, stderr=stderr)

    def require_trustworthy_status(self, serial: str) -> None:
        self.calls.append("protocol")
        if self.legacy:
            raise importer.ImportFailure("legacy ADB has no trustworthy remote status")
        if serial != self.binding.serial:
            raise importer.ImportFailure("wrong serial")

    def observe(self, bound: importer.Binding) -> importer.LiveState:
        self.calls.append("observe")
        if self.disconnect_after_attestation_once and self._attestation_completed:
            self.disconnect_after_attestation_once = False
            self._attestation_completed = False
            raise importer.ImportFailure("synthetic disconnect after attestation")
        return copy.deepcopy(self.state)

    def activate_writable(self, bound: importer.Binding) -> importer.RemoteResult:
        self.calls.append("activate")
        self.state = dataclass_replace(
            self.state,
            pool_data_percent=self.state.pool_data_percent or "0.00",
            pool_metadata_percent=self.state.pool_metadata_percent or "0.00",
            disk_active=True,
            disk_dm_uuid=bound.disk_dm_uuid,
            disk_sectors=bound.disk_sectors,
            disk_kernel_ro=False,
        )
        return self.phase_result("activate", importer.activate_writable_argv(bound))

    def write_extent(self, bound: importer.Binding, disk: importer.HeldFile, extent: importer.Extent) -> importer.RemoteResult:
        self.calls.append(f"extent-{extent.ordinal}")
        self.writes.append(extent.ordinal)
        if self.fail_extent_once == extent.ordinal:
            self.fail_extent_once = None
            return self.result(importer.write_extent_argv(bound, extent), 5)
        if disk.hash_region(extent.source_offset, extent.source_bytes) != extent.sha256:
            return self.result(importer.write_extent_argv(bound, extent), 9)
        self.completed_extents.add(extent.ordinal)
        if self.drift_after_extent is not None and self.drift_after_extent[0] == extent.ordinal:
            _, data_percent, metadata_percent = self.drift_after_extent
            self.state = dataclass_replace(
                self.state,
                pool_data_percent=data_percent,
                pool_metadata_percent=metadata_percent,
            )
        return self.phase_result(
            f"extent-{extent.ordinal}",
            importer.write_extent_argv(bound, extent),
            stdout=importer.extent_success_stdout(extent),
        )

    def verify_extent(self, bound: importer.Binding, extent: importer.Extent) -> importer.RemoteResult:
        self.calls.append(f"verify-extent-{extent.ordinal}")
        rc = 0 if extent.ordinal in self.completed_extents and extent.ordinal not in self.corrupt_extents else 8
        return self.result(
            importer.verify_extent_argv(bound, extent),
            rc,
            stdout=importer.extent_readback_stdout(extent),
        )

    def sync(self, bound: importer.Binding) -> importer.RemoteResult:
        self.calls.append("sync")
        if self.drift_after_sync == "identity":
            self.state = dataclass_replace(self.state, pool_uuid=DATA_UUID)
        if self.drift_after_sync == "pool":
            self.state = dataclass_replace(self.state, pool_data_percent="30.00")
        return self.phase_result("sync", importer.sync_import_argv(bound))

    def make_readonly(self, bound: importer.Binding) -> importer.RemoteResult:
        self.calls.append("make-readonly")
        self.state = dataclass_replace(
            self.state,
            disk_active=True,
            disk_dm_uuid=bound.disk_dm_uuid,
            disk_sectors=bound.disk_sectors,
            disk_kernel_ro=True,
        )
        return self.phase_result("make-readonly", importer.make_readonly_argv(bound))

    def attest(self, bound: importer.Binding, expected_sha256: str):
        self.calls.append("attest")
        self._attestation_completed = True
        actual = "0" * 64 if self.bad_attestation else expected_sha256
        attestation = {
            "schema": importer.ATTEST_SCHEMA,
            "serial": bound.serial,
            "userdata_partuuid": bound.partuuid,
            "vg_uuid": bound.vg_uuid,
            "lv_uuid": bound.disk_uuid,
            "lvm_dm_uuid": bound.disk_dm_uuid,
            "sectors": bound.disk_sectors,
            "bytes": bound.disk_bytes,
            "ro": True,
            "sha_applet": "/bin/busybox",
            "expected_sha256": expected_sha256,
            "actual_sha256": actual,
        }
        return self.phase_result(
            "attest",
            importer.attest_argv(bound, expected_sha256),
            stdout=f"FRANKENSARGO_DURANIUM_SHA256_V1|{expected_sha256}\n".encode(),
        ), attestation

    def deactivate(self, bound: importer.Binding) -> importer.RemoteResult:
        self.calls.append("deactivate")
        self.state = dataclass_replace(
            self.state,
            disk_active=False,
            disk_dm_uuid=None,
            disk_sectors=None,
            disk_kernel_ro=None,
        )
        return self.phase_result("deactivate", importer.deactivate_argv(bound))

    def publish(self, bound: importer.Binding) -> importer.RemoteResult:
        self.calls.append("publish")
        self.state = dataclass_replace(
            self.state,
            disk_tags=importer.REQUIRED_PUBLISHED_TAGS,
            disk_permission="r",
        )
        if self.disconnect_after_publication_once:
            self.disconnect_after_publication_once = False
            raise importer.ImportFailure("synthetic disconnect after publication")
        return self.phase_result("publish", importer.publish_argv(bound))

    def capture_vgcfg(self, bound: importer.Binding):
        self.calls.append("vgcfg")
        ids = "\n".join(f'    id = "{identifier}"' for identifier in bound.all_lvm_uuids)
        data = f"franken {{\n{ids}\n}}\n".encode()
        return self.phase_result(
            "vgcfg", importer.capture_vgcfg_argv(bound), stdout=data
        ), data


def dataclass_replace(value, **changes):
    return importer.dataclasses.replace(value, **changes)


class Harness:
    def __init__(self, root: Path, *, two_extents: bool = False):
        self.root = root
        self.bound = binding()
        self.inputs = Inputs(root / "inputs", two_extents=two_extents)
        self.remote = FakeRemote(self.bound)
        self.state = root / "state"
        self.journal: importer.Journal | None = None

    def controller(self, *, implementation_hashes: dict[str, str] | None = None) -> importer.ImportController:
        self.journal = importer.Journal(self.state, allow_volatile=True)
        if implementation_hashes is None:
            implementation_hashes = {
                "entrypoint": "a" * 64,
                "controller": "b" * 64,
                "shell_v2": "c" * 64,
                "audit_tool": self.inputs.tool.sha256,
            }
        return importer.ImportController(
            binding=self.bound,
            contract=self.inputs.contract,
            disk=self.inputs.disk,
            provenance=self.inputs.provenance,
            adapter=self.inputs.adapter,
            pocketboot_image=self.inputs.pocketboot_image,
            pocketboot_provenance=self.inputs.pocketboot_provenance,
            pocketboot_runtime={"synthetic": True},
            remote=self.remote,
            journal=self.journal,
            audit_tool_sha256=self.inputs.tool.sha256,
            implementation_hashes=implementation_hashes,
        )

    def close_journal(self) -> None:
        if self.journal is not None:
            self.journal.close()
            self.journal = None

    def close(self) -> None:
        self.close_journal()
        self.inputs.close()


class DuraniumImportControllerTests(unittest.TestCase):
    def test_bound_pocketboot_image_provenance_and_safety_patch_set_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "pocketboot-sargo-lvm-bound.img"
            provenance_path = root / "pocketboot-sargo-lvm-bound.img.provenance.json"
            image_path.write_bytes(b"bound image")
            patches = [
                {"name": path.name, "sha256": digest(path.read_bytes())}
                for path in sorted((ROOT / "patches/pocketboot").glob("*.patch"))
            ]
            value = {
                "format": importer.POCKETBOOT_PROVENANCE_SCHEMA,
                "profile": "interim-lvm-bound-lab",
                "binding": {
                    "vg_uuid": VG_UUID,
                    "pv_partuuids": [PARTUUID],
                    "kernel_cmdline": (
                        "pocketboot.log=debug sysrq_always_enabled=1 "
                        f"pocketboot.vg_uuid={VG_UUID} pocketboot.pv_partuuid={PARTUUID}"
                    ),
                },
                "patches": patches,
                "output": {
                    "basename": image_path.name,
                    "bytes": image_path.stat().st_size,
                    "sha256": digest(image_path.read_bytes()),
                },
            }
            provenance_path.write_bytes(importer.canonical_bytes(value))
            image = importer.HeldFile.open("image", image_path)
            provenance = importer.HeldFile.open("provenance", provenance_path)
            try:
                observed = importer.validate_pocketboot_runtime(
                    image, provenance, binding(), ROOT / "patches/pocketboot"
                )
                self.assertEqual(observed["image"]["sha256"], image.sha256)
            finally:
                provenance.close()
                image.close()

            value["patches"] = [item for item in patches if item["name"] != "0013-adb-shell-v2-status.patch"]
            provenance_path.write_bytes(importer.canonical_bytes(value))
            image = importer.HeldFile.open("image", image_path)
            provenance = importer.HeldFile.open("provenance", provenance_path)
            try:
                with self.assertRaisesRegex(importer.ImportFailure, "patch set differs"):
                    importer.validate_pocketboot_runtime(
                        image, provenance, binding(), ROOT / "patches/pocketboot"
                    )
            finally:
                provenance.close()
                image.close()

    def test_production_adapter_uses_shared_shell_v2_and_file_backed_stdin(self) -> None:
        calls: list[tuple[object, ...]] = []

        class Client:
            def __init__(self, executable: str, serial: str):
                calls.append(("init", executable, serial))

            def verify(self, *, timeout: int | None = None) -> None:
                calls.append(("verify", timeout))

            def run(self, argv, *, stdin=None, timeout=None):
                calls.append(("run", tuple(argv), stdin, timeout))
                return types.SimpleNamespace(
                    argv=tuple(argv), returncode=0, stdout=b"out", stderr=b"err"
                )

        stream = io.BytesIO(b"bounded extent")
        with mock.patch.object(importer, "AdbShellV2", Client):
            remote = importer.AdbShellV2Remote("adb33", SERIAL)
            remote.require_trustworthy_status(SERIAL)
            result = remote.run(["/bin/dd", "of=/dev/dm-1"], input_file=stream, timeout=77)
        self.assertEqual(calls[0], ("init", "adb33", SERIAL))
        self.assertEqual(calls[1], ("verify", 30))
        self.assertEqual(calls[2], ("run", ("/bin/dd", "of=/dev/dm-1"), stream, 77))
        self.assertEqual(result.returncode, 0)
        self.assertTrue(result.trustworthy_remote_status)
        self.assertFalse(any("exec-in" in str(item) or "exec-out" in str(item) for call in calls for item in call))

        class Broken(Client):
            def verify(self, *, timeout: int | None = None) -> None:
                raise importer.ShellV2Error("legacy status zero")

        with mock.patch.object(importer, "AdbShellV2", Broken):
            remote = importer.AdbShellV2Remote("adb33", SERIAL)
            with self.assertRaisesRegex(importer.ImportFailure, "legacy or ambiguous ADB is refused"):
                remote.require_trustworthy_status(SERIAL)

        class WrongArgv(Client):
            def run(self, argv, *, stdin=None, timeout=None):
                return types.SimpleNamespace(
                    argv=("/bin/true",), returncode=0, stdout=b"", stderr=b""
                )

        with mock.patch.object(importer, "AdbShellV2", WrongArgv):
            remote = importer.AdbShellV2Remote("adb33", SERIAL)
            remote.require_trustworthy_status(SERIAL)
            with self.assertRaisesRegex(importer.ImportFailure, "other than the requested exact command"):
                remote.run(["/bin/false"])

    def test_production_remote_regenerates_all_mutation_attest_publish_capture_argv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Inputs(Path(temporary))
            bound = binding()
            extent = inputs.contract.extents[0]
            remote = importer.AdbShellV2Remote.__new__(importer.AdbShellV2Remote)
            remote.serial = SERIAL
            remote.qualified = True
            observed: list[tuple[str, ...]] = []
            streamed: list[bytes] = []
            vgcfg = (
                "franken {\n"
                + "\n".join(f'  id = "{item}"' for item in bound.all_lvm_uuids)
                + "\n}\n"
            ).encode()

            def run(argv, *, input_file=None, timeout=120):
                del timeout
                exact = tuple(argv)
                observed.append(exact)
                if input_file is not None:
                    streamed.append(input_file.read())
                if exact == importer.attest_argv(bound, inputs.contract.full_lv_sha256):
                    stdout = (
                        f"FRANKENSARGO_DURANIUM_SHA256_V1|"
                        f"{inputs.contract.full_lv_sha256}\n"
                    ).encode()
                elif exact == importer.write_extent_argv(bound, extent):
                    stdout = importer.extent_success_stdout(extent)
                elif exact == importer.verify_extent_argv(bound, extent):
                    stdout = importer.extent_readback_stdout(extent)
                elif exact == importer.capture_vgcfg_argv(bound):
                    stdout = vgcfg
                else:
                    stdout = b""
                return importer.RemoteResult(exact, 0, stdout, b"", True)

            try:
                with mock.patch.object(remote, "run", side_effect=run):
                    remote.activate_writable(bound)
                    remote.write_extent(bound, inputs.disk, extent)
                    remote.verify_extent(bound, extent)
                    remote.sync(bound)
                    remote.make_readonly(bound)
                    remote.attest(bound, inputs.contract.full_lv_sha256)
                    remote.deactivate(bound)
                    remote.publish(bound)
                    result, data = remote.capture_vgcfg(bound)
                expected = [
                    importer.activate_writable_argv(bound),
                    importer.write_extent_argv(bound, extent),
                    importer.verify_extent_argv(bound, extent),
                    importer.sync_import_argv(bound),
                    importer.make_readonly_argv(bound),
                    importer.attest_argv(bound, inputs.contract.full_lv_sha256),
                    importer.deactivate_argv(bound),
                    importer.publish_argv(bound),
                    importer.capture_vgcfg_argv(bound),
                ]
                self.assertEqual(observed, expected)
                self.assertEqual(streamed, [b"A" + b"\0" * 511])
                self.assertEqual(result.stdout, vgcfg)
                self.assertEqual(data, vgcfg)
            finally:
                inputs.close()

    def test_production_attest_typed_nonzero_is_journaled_and_never_replayed(self) -> None:
        for stdout in (b"", b"malformed verifier output\n"):
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                production = importer.AdbShellV2Remote.__new__(importer.AdbShellV2Remote)
                production.serial = SERIAL
                production.qualified = True
                attest_calls: list[tuple[str, ...]] = []

                def run(argv, *, input_file=None, timeout=120):
                    del input_file, timeout
                    exact = tuple(argv)
                    attest_calls.append(exact)
                    return importer.RemoteResult(
                        exact, 1, stdout, b"target verifier failed\n", True
                    )

                def production_attest(bound, expected_sha256):
                    return importer.AdbShellV2Remote.attest(
                        production, bound, expected_sha256
                    )

                harness.remote.attest = production_attest  # type: ignore[method-assign]
                try:
                    with mock.patch.object(production, "run", side_effect=run):
                        with self.assertRaisesRegex(
                            importer.ImportFailure, "does not prove a successful"
                        ):
                            harness.controller().execute()
                        result_path = (
                            harness.state
                            / "events/05-full-lv-attestation.result.json"
                        )
                        record = json.loads(result_path.read_text())
                        self.assertEqual(record["remote_result"]["returncode"], 1)
                        self.assertEqual(
                            record["remote_result"]["stdout_sha256"], digest(stdout)
                        )
                        self.assertFalse(record["attestation"]["remote_success"])
                        harness.close_journal()

                        with self.assertRaisesRegex(
                            importer.ImportFailure, "does not prove a successful"
                        ):
                            harness.controller().execute()
                    self.assertEqual(
                        attest_calls,
                        [
                            importer.attest_argv(
                                harness.bound, harness.inputs.contract.full_lv_sha256
                            )
                        ],
                    )
                    self.assertNotIn("publish", harness.remote.calls)
                finally:
                    harness.close()

    def test_production_observe_is_exact_serial_fenced_readonly_and_typed(self) -> None:
        bound = binding()
        remote = importer.AdbShellV2Remote.__new__(importer.AdbShellV2Remote)
        remote.serial = SERIAL
        remote.qualified = True
        calls: list[tuple[str, ...]] = []
        pvs = {
            "report": [{"pv": [{
                "pv_uuid": bound.pv_uuid,
                "pv_name": bound.anchor,
                "vg_uuid": bound.vg_uuid,
                "vg_name": "franken",
            }]}]
        }
        common = {"vg_uuid": bound.vg_uuid}
        lvs = {
            "report": [{"lv": [
                {
                    **common,
                    "lv_uuid": bound.pool_uuid,
                    "lv_name": "pool",
                    "lv_size": str(bound.pool_bytes),
                    "lv_active": "active",
                    "lv_permissions": "writeable",
                    "lv_tags": "greygoo.replaceable,greygoo.thin-pool.v1",
                    "lv_attr": "twi-a-tz--",
                    "segtype": "thin-pool",
                    "pool_lv_uuid": "",
                    "data_lv_uuid": bound.pool_data_uuid,
                    "metadata_lv_uuid": bound.pool_metadata_uuid,
                    "data_percent": "0.00",
                    "metadata_percent": "0.00",
                    "discards": "nopassdown",
                    "lv_when_full": "error",
                    "lv_health_status": "",
                    "chunk_size": str(bound.chunk_bytes),
                },
                {**common, "lv_uuid": bound.pool_data_uuid, "lv_name": "[pool_tdata]"},
                {**common, "lv_uuid": bound.pool_metadata_uuid, "lv_name": "[pool_tmeta]"},
                {
                    **common,
                    "lv_uuid": bound.disk_uuid,
                    "lv_name": "disk-duranium",
                    "lv_size": str(bound.disk_bytes),
                    "lv_active": "inactive",
                    "lv_permissions": "writeable",
                    "lv_tags": ",".join(sorted(importer.REQUIRED_PENDING_TAGS)),
                    "lv_attr": "Vwi---tz--",
                    "segtype": "thin",
                    "pool_lv_uuid": bound.pool_uuid,
                },
            ]}]
        }

        def run(argv, *, input_file=None, timeout=120):
            del input_file, timeout
            exact = tuple(argv)
            calls.append(exact)
            if exact[:2] == ("/bin/sh", "-c"):
                output = (bound.anchor + "\n").encode()
            elif len(exact) > 1 and exact[1] == "pvs":
                output = importer.canonical_bytes(pvs)
            elif len(exact) > 1 and exact[1] == "lvs":
                output = importer.canonical_bytes(lvs)
            else:
                raise AssertionError(exact)
            return importer.RemoteResult(exact, 0, output, b"", True)

        with mock.patch.object(remote, "run", side_effect=run):
            state = remote.observe(bound)
        self.assertEqual(state.pool_uuid, bound.pool_uuid)
        self.assertEqual(state.pool_metadata_percent, "0.00")
        self.assertEqual(len(calls), 3)
        self.assertIn(bound.partuuid, calls[0][2])
        for argv in calls[1:]:
            self.assertIn("--readonly", argv)
            self.assertIn("--nolocking", argv)
            self.assertIn(bound.anchor, argv)

    def test_final_bootstrap_checkpoint_and_vgcfg_are_independently_bound(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "bootstrap-state"
            steps = state / "steps"
            steps.mkdir(parents=True)
            vgcfg = state / "franken.vgcfg"
            all_ids = (PV_UUID, VG_UUID, POOL_UUID, DATA_UUID, META_UUID, DISK_UUID)
            vgcfg_data = (
                "franken {\n"
                + "\n".join(f'  id = "{identifier}"' for identifier in all_ids)
                + "\n}\n"
            ).encode()
            vgcfg.write_bytes(vgcfg_data)
            plan_core = {
                "schema": importer.PLAN_SCHEMA,
                "action": "bootstrap-userdata-anchor",
                "operation_uuid": "01234567-89ab-4cde-8f01-23456789abcd",
                "device": {"fastboot_serial": SERIAL, "product": "sargo", "compatible": "google,sargo"},
                "partition": {"partuuid": PARTUUID, "kernel_name_observation": "mmcblk0p72"},
                "lvm": {
                    "vg_name": "franken",
                    "pv": {"planned_uuid": PV_UUID},
                    "thin_pool": {
                        "name": "pool",
                        "chunk_bytes": str(256 * 1024),
                        "discard_policy": "nopassdown",
                        "error_when_full": True,
                    },
                    "volumes": [
                        {
                            "name": "pool",
                            "kind": "thin-data",
                            "size_bytes": str(VIRTUAL_BYTES),
                            "tags": ["greygoo.replaceable", "greygoo.thin-pool.v1"],
                        },
                        {
                            "name": "disk-duranium",
                            "kind": "thin",
                            "virtual_bytes": str(VIRTUAL_BYTES),
                            "allocation": {"thin_pool": "pool", "policy": "thin-only"},
                            "tags": sorted(importer.REQUIRED_PENDING_TAGS),
                        },
                    ],
                },
                "transaction": {
                    "post_import_argv": [
                        {
                            "step": "publish-verified-duranium-disk",
                            "argv": [
                                "/sbin/lvm.static", "lvchange", "--devices", "@USERDATA_BLOCK_DEVICE@",
                                "--nohints", "--config", importer.REPORT_CONFIG, "--permission", "r",
                                "--deltag", "greygoo.import-pending", "--addtag", "pocketboot.disk.v1",
                                "franken/disk-duranium",
                            ],
                        }
                    ]
                },
            }
            authorization = "sha256:" + digest(importer.canonical_bytes(plan_core))
            plan_value = {
                **plan_core,
                "authorization_sha256": authorization,
                "confirmation": {"required_by_future_executor": True, "token": "unused"},
            }
            plan_path = root / "plan.json"
            plan_path.write_bytes(importer.canonical_bytes(plan_value))
            lv_ids = {
                "pool": POOL_UUID,
                "pool_tdata": DATA_UUID,
                "pool_tmeta": META_UUID,
                "disk-duranium": DISK_UUID,
            }
            lvm_state = {
                "pvs": [{"pv_uuid": PV_UUID, "pv_name": "/dev/mmcblk0p72", "vg_uuid": VG_UUID}],
                "vgs": [{"vg_uuid": VG_UUID, "vg_name": "franken"}],
                "lvs": [
                    {
                        "vg_uuid": VG_UUID, "lv_uuid": POOL_UUID, "lv_name": "pool",
                        "lv_size": str(VIRTUAL_BYTES), "segtype": "thin-pool",
                        "data_lv_uuid": DATA_UUID, "metadata_lv_uuid": META_UUID,
                        "discards": "nopassdown", "lv_when_full": "error", "lv_attr": "twi-------",
                    },
                    {"vg_uuid": VG_UUID, "lv_uuid": DATA_UUID, "lv_name": "[pool_tdata]"},
                    {"vg_uuid": VG_UUID, "lv_uuid": META_UUID, "lv_name": "[pool_tmeta]"},
                    {
                        "vg_uuid": VG_UUID, "lv_uuid": DISK_UUID, "lv_name": "disk-duranium",
                        "lv_size": str(VIRTUAL_BYTES), "segtype": "thin", "pool_lv_uuid": POOL_UUID,
                        "lv_tags": sorted(importer.REQUIRED_PENDING_TAGS), "lv_attr": "Vwi-------",
                    },
                ],
            }
            ids = {"pv_uuid": PV_UUID, "vg_uuid": VG_UUID, "lv_uuids": lv_ids}
            checkpoint_value = {
                "schema": importer.CHECKPOINT_SCHEMA,
                "authorization_sha256": authorization,
                "operation_uuid": plan_core["operation_uuid"],
                "ordinal": 12,
                "stage": 11,
                "step": "backup-vg-metadata",
                "recovered_exact_postcondition": False,
                "lvm_state_sha256": "sha256:" + digest(importer.canonical_bytes(lvm_state)),
                "lvm_state": lvm_state,
                "generated_ids": ids,
                "vgcfgbackup": {
                    "bytes": str(len(vgcfg_data)),
                    "sha256": "sha256:" + digest(vgcfg_data),
                    "generated_ids": ids,
                    "remote_source": "/run/franken.vgcfg",
                    "host_destination": str(vgcfg),
                },
            }
            checkpoint_path = steps / "12-backup-vg-metadata.json"
            checkpoint_path.write_bytes(importer.canonical_bytes(checkpoint_value))
            audit = {
                "destination": {"virtual_bytes": VIRTUAL_BYTES},
                "thin_pool_gate": {
                    "pool_bytes": VIRTUAL_BYTES,
                    "chunk_bytes": 256 * 1024,
                    "required_minimum_pool_free_bytes": 16 * 1024 * 1024 * 1024,
                    "maximum_pool_metadata_percent": "75.00",
                },
            }
            plan_file = importer.HeldFile.open("plan", plan_path)
            checkpoint_file = importer.HeldFile.open("checkpoint", checkpoint_path)
            try:
                observed = importer.validate_plan_checkpoint(
                    plan_file, checkpoint_file, SERIAL, PARTUUID, audit
                )
                self.assertEqual(observed.disk_uuid, DISK_UUID)
                self.assertEqual(observed.all_lvm_uuids, tuple(sorted(all_ids)))
            finally:
                checkpoint_file.close()
                plan_file.close()

            checkpoint_value["vgcfgbackup"]["sha256"] = "sha256:" + "0" * 64
            checkpoint_path.write_bytes(importer.canonical_bytes(checkpoint_value))
            plan_file = importer.HeldFile.open("plan", plan_path)
            checkpoint_file = importer.HeldFile.open("checkpoint", checkpoint_path)
            try:
                with self.assertRaisesRegex(importer.ImportFailure, "vgcfgbackup file differs"):
                    importer.validate_plan_checkpoint(
                        plan_file, checkpoint_file, SERIAL, PARTUUID, audit
                    )
            finally:
                checkpoint_file.close()
                plan_file.close()

    def test_success_is_resumable_and_publishes_only_after_full_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary), two_extents=True)
            try:
                result = harness.controller().execute()
                self.assertTrue(result["published"])
                self.assertEqual(harness.remote.writes, [0, 1])
                self.assertLess(harness.remote.calls.index("attest"), harness.remote.calls.index("publish"))
                self.assertLess(harness.remote.calls.index("publish"), harness.remote.calls.index("vgcfg"))
                harness.close_journal()
                calls = list(harness.remote.calls)
                resumed = harness.controller().execute()
                self.assertEqual(resumed, result)
                self.assertEqual(harness.remote.writes, [0, 1])
                self.assertNotIn("publish", harness.remote.calls[len(calls) :])
            finally:
                harness.close()

    def test_inactive_unknown_pool_usage_is_gated_after_activation_before_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.state = dataclass_replace(
                    harness.remote.state,
                    pool_data_percent="",
                    pool_metadata_percent="",
                )
                result = harness.controller().execute()
                self.assertTrue(result["published"])
                pre_write = harness.journal.read("pre-write-state.json")
                self.assertEqual(pre_write["state"]["pool_data_percent"], "0.00")
                self.assertLess(harness.remote.calls.index("activate"), harness.remote.calls.index("extent-0"))
            finally:
                harness.close()

    def test_typed_nonzero_extent_is_durable_and_never_replayed_as_a_disconnect(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.fail_extent_once = 0
                with self.assertRaisesRegex(importer.ImportFailure, "failed remotely"):
                    harness.controller().execute()
                self.assertNotIn("publish", harness.remote.calls)
                harness.close_journal()
                with self.assertRaises(importer.ImportFailure):
                    harness.controller().execute()
                self.assertEqual(harness.remote.writes, [0])
            finally:
                harness.close()

    def test_resume_recovers_attestation_and_publication_crash_windows(self) -> None:
        for window in ("attestation", "publication"):
            with self.subTest(window=window), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    if window == "attestation":
                        harness.remote.disconnect_after_attestation_once = True
                    else:
                        harness.remote.disconnect_after_publication_once = True
                    with self.assertRaisesRegex(importer.ImportFailure, "synthetic disconnect"):
                        harness.controller().execute()
                    harness.close_journal()
                    result = harness.controller().execute()
                    self.assertTrue(result["published"])
                    self.assertEqual(harness.remote.writes, [0])
                    if window == "publication":
                        self.assertEqual(harness.remote.calls.count("publish"), 1)
                finally:
                    harness.close()

    def test_corrupt_audit_is_not_trusted_over_independent_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Inputs(Path(temporary))
            try:
                provided = copy.deepcopy(inputs.audit)
                provided["binding"]["published_tag"] = "corrupt.tag"
                with self.assertRaisesRegex(importer.ImportFailure, "differs from a fresh independent audit"):
                    importer.independently_reaudit(
                        provided,
                        binding(),
                        inputs.disk,
                        inputs.provenance,
                        inputs.adapter,
                        inputs.tool,
                        runner=lambda argv, tool: copy.deepcopy(inputs.audit),
                    )
            finally:
                inputs.close()

    def test_changed_source_fails_before_remote_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                controller = harness.controller()
                harness.inputs.disk_path.write_bytes(b"X" * 512)
                with self.assertRaisesRegex(importer.ImportFailure, "changed while held"):
                    controller.execute()
                self.assertNotIn("activate", harness.remote.calls)
            finally:
                harness.close()

    def test_extent_journal_rejects_skips_reordering_and_partial_results(self) -> None:
        for mutation in ("skip", "reorder", "partial"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary), two_extents=True)
                try:
                    controller = harness.controller()
                    first, second = harness.inputs.contract.extents
                    if mutation == "skip":
                        record = {
                            "schema": importer.JOURNAL_SCHEMA,
                            "transaction_sha256": controller.transaction,
                            "kind": "sparse-write-extent",
                            "previous_record_sha256": controller.transaction,
                            "extent": second.json(),
                            "argv": list(importer.write_extent_argv(harness.bound, second)),
                        }
                        importer.atomic_write(harness.state / "extents/00000001.intent.json", importer.canonical_bytes(record))
                    else:
                        intent = {
                            "schema": importer.JOURNAL_SCHEMA,
                            "transaction_sha256": controller.transaction,
                            "kind": "sparse-write-extent",
                            "previous_record_sha256": controller.transaction,
                            "extent": first.json(),
                            "argv": list(importer.write_extent_argv(harness.bound, first)),
                        }
                        if mutation == "reorder":
                            intent["extent"] = second.json()
                        importer.atomic_write(harness.state / "extents/00000000.intent.json", importer.canonical_bytes(intent))
                        if mutation == "partial":
                            intent_sha = importer.sha256_bytes(importer.canonical_bytes(intent))
                            result = importer._result_json(
                                harness.remote.result(
                                    importer.write_extent_argv(harness.bound, first),
                                    stdout=importer.extent_success_stdout(first),
                                ),
                                controller.transaction,
                                intent_sha,
                            )
                            result.update(
                                {
                                    "source_bytes": 1,
                                    "source_sha256": first.sha256,
                                    "target_bytes": first.source_bytes,
                                    "target_readback_sha256": first.sha256,
                                    "durability_barrier": "sync+blockdev-flushbufs+readback-v1",
                                }
                            )
                            importer.atomic_write(harness.state / "extents/00000000.result.json", importer.canonical_bytes(result))
                    with self.assertRaises(importer.ImportFailure):
                        controller._validate_extent_journal()
                finally:
                    harness.close()

    def test_stale_journal_binding_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                controller = harness.controller()
                stale = {
                    "schema": importer.JOURNAL_SCHEMA,
                    "transaction_sha256": "0" * 64,
                    "phase": "00-activate-writable",
                    "kind": "activate-exact-pending-lv",
                }
                importer.atomic_write(
                    harness.state / "events/00-activate-writable.intent.json",
                    importer.canonical_bytes(stale),
                )
                with self.assertRaisesRegex(importer.ImportFailure, "existing journal record differs"):
                    controller.execute()
                self.assertNotIn("publish", harness.remote.calls)
            finally:
                harness.close()

    def test_nonzero_or_untrustworthy_remote_status_never_publishes(self) -> None:
        for kind in ("nonzero", "legacy"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    if kind == "nonzero":
                        harness.remote.fail_extent_once = 0
                    else:
                        harness.remote.legacy = True
                    with self.assertRaises(importer.ImportFailure):
                        harness.controller().execute()
                    self.assertNotIn("publish", harness.remote.calls)
                finally:
                    harness.close()

    def test_identity_or_pool_drift_stops_before_hash_and_publication(self) -> None:
        for drift in ("identity", "pool"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    harness.remote.drift_after_sync = drift
                    with self.assertRaises(importer.ImportFailure):
                        harness.controller().execute()
                    self.assertNotIn("attest", harness.remote.calls)
                    self.assertNotIn("publish", harness.remote.calls)
                finally:
                    harness.close()

    def test_metadata_99_is_rejected_before_any_extent_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.state = dataclass_replace(
                    harness.remote.state, pool_metadata_percent="99.00"
                )
                with self.assertRaisesRegex(
                    importer.ImportFailure, "metadata usage.*exceeds"
                ):
                    harness.controller().execute()
                self.assertEqual(harness.remote.writes, [])
                self.assertNotIn("attest", harness.remote.calls)
                self.assertNotIn("publish", harness.remote.calls)
            finally:
                harness.close()

    def test_each_remaining_extent_uses_fresh_live_pool_usage_and_reserve(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary), two_extents=True)
            try:
                # 19.99 with 0.01 report precision has a conservative upper
                # bound of exactly 20%; even one remaining 512-byte chunk then
                # breaches the 4 GiB usable-data / 16 GiB reserve boundary.
                harness.remote.drift_after_extent = (0, "19.99", "0.00")
                with self.assertRaisesRegex(
                    importer.ImportFailure, "remaining audited allocation"
                ):
                    harness.controller().execute()
                self.assertEqual(harness.remote.writes, [0])
                self.assertNotIn("extent-1", harness.remote.calls)
                self.assertNotIn("attest", harness.remote.calls)
            finally:
                harness.close()

    def test_extent_success_is_flushed_read_back_and_live_verified_on_resume(self) -> None:
        for corrupt in (False, True):
            with self.subTest(corrupt=corrupt), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    controller = harness.controller()
                    original = harness.journal.write_once
                    crashed = False

                    def crash_after_durable_result(relative, value):
                        nonlocal crashed
                        stored = original(relative, value)
                        if relative == "extents/00000000.result.json" and not crashed:
                            crashed = True
                            raise importer.ImportFailure("synthetic host crash after extent result fsync")
                        return stored

                    with mock.patch.object(
                        harness.journal, "write_once", side_effect=crash_after_durable_result
                    ):
                        with self.assertRaisesRegex(importer.ImportFailure, "host crash"):
                            controller.execute()
                    harness.close_journal()
                    if corrupt:
                        harness.remote.corrupt_extents.add(0)
                        with self.assertRaisesRegex(importer.ImportFailure, "resume readback"):
                            harness.controller().execute()
                        self.assertNotIn("publish", harness.remote.calls)
                    else:
                        result = harness.controller().execute()
                        self.assertTrue(result["published"])
                        self.assertEqual(harness.remote.writes, [0])
                        self.assertIn("verify-extent-0", harness.remote.calls)
                finally:
                    harness.close()

    def test_disconnect_before_host_extent_result_replays_only_that_exact_extent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.disconnect_phase_once = "extent-0"
                with self.assertRaisesRegex(importer.ImportFailure, "disconnect after extent-0"):
                    harness.controller().execute()
                harness.close_journal()
                result = harness.controller().execute()
                self.assertTrue(result["published"])
                self.assertEqual(harness.remote.writes, [0, 0])
            finally:
                harness.close()

    def test_typed_nonzero_is_durable_for_every_remote_phase(self) -> None:
        for phase in (
            "activate", "extent-0", "sync", "make-readonly", "attest",
            "deactivate", "publish", "vgcfg",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    harness.remote.fail_phase_once = phase
                    with self.assertRaises(importer.ImportFailure):
                        harness.controller().execute()
                    self.assertFalse(
                        (harness.state / "events/12-complete.state.json").exists()
                    )
                    harness.close_journal()
                    calls = list(harness.remote.calls)
                    previous_phase_calls = calls.count(phase)
                    with self.assertRaises(importer.ImportFailure):
                        harness.controller().execute()
                    # A typed nonzero result is not reclassified as a transport
                    # crash and the command is never replayed on restart.
                    self.assertEqual(
                        harness.remote.calls.count(phase), previous_phase_calls
                    )
                    if phase == "extent-0":
                        self.assertEqual(harness.remote.writes, [0])
                    if phase == "publish":
                        self.assertEqual(harness.remote.calls.count("publish"), 1)
                    if phase == "vgcfg":
                        self.assertEqual(harness.remote.calls.count("vgcfg"), 1)
                finally:
                    harness.close()

    def test_disconnect_windows_resume_all_remote_phases_without_skipping_gates(self) -> None:
        for phase in (
            "activate", "extent-0", "sync", "make-readonly", "attest",
            "deactivate", "publish", "vgcfg",
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                harness = Harness(Path(temporary))
                try:
                    harness.remote.disconnect_phase_once = phase
                    with self.assertRaisesRegex(importer.ImportFailure, "synthetic disconnect"):
                        harness.controller().execute()
                    harness.close_journal()
                    result = harness.controller().execute()
                    self.assertTrue(result["published"])
                    if phase == "extent-0":
                        self.assertEqual(harness.remote.writes, [0, 0])
                    if phase in {"activate", "make-readonly", "deactivate", "publish"}:
                        self.assertEqual(harness.remote.calls.count(phase), 1)
                    if phase in {"sync", "attest", "vgcfg"}:
                        self.assertEqual(harness.remote.calls.count(phase), 2)
                finally:
                    harness.close()

    def test_vgcfgbackup_host_file_before_result_crash_is_exactly_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                controller = harness.controller()
                original = harness.journal.write_once
                crashed = False

                def crash_before_vgcfg_result(relative, value):
                    nonlocal crashed
                    if relative == "events/11-vgcfgbackup.result.json" and not crashed:
                        crashed = True
                        self.assertTrue(
                            (harness.state / "franken-post-import.vgcfg").exists()
                        )
                        raise importer.ImportFailure("synthetic host crash before vgcfg result")
                    return original(relative, value)

                with mock.patch.object(
                    harness.journal, "write_once", side_effect=crash_before_vgcfg_result
                ):
                    with self.assertRaisesRegex(importer.ImportFailure, "before vgcfg result"):
                        controller.execute()
                harness.close_journal()
                result = harness.controller().execute()
                self.assertTrue(result["published"])
                self.assertEqual(harness.remote.calls.count("vgcfg"), 2)
            finally:
                harness.close()

    def test_same_transaction_rejects_stale_true_result_and_weaker_controller_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                controller = harness.controller()
                argv = importer.activate_writable_argv(harness.bound)
                intent, intent_sha = controller._phase_intent(
                    "00-activate-writable",
                    {"kind": "activate-exact-pending-lv", "argv": list(argv)},
                )
                del intent
                stale = importer._result_json(
                    importer.RemoteResult(("/bin/true",), 0, b"", b"", True),
                    controller.transaction,
                    intent_sha,
                )
                importer.atomic_write(
                    harness.state / "events/00-activate-writable.result.json",
                    importer.canonical_bytes(stale),
                )
                with self.assertRaisesRegex(importer.ImportFailure, "argv differs"):
                    controller.execute()
                harness.close_journal()

                # The code hashes are part of intent.json and therefore of the
                # transaction digest.  An older/weaker implementation cannot
                # reopen the same state directory under a new self-description.
                weaker = {
                    "entrypoint": "a" * 64,
                    "controller": "0" * 64,
                    "shell_v2": "c" * 64,
                    "audit_tool": harness.inputs.tool.sha256,
                }
                with self.assertRaisesRegex(importer.ImportFailure, "intent.json"):
                    harness.controller(implementation_hashes=weaker)
            finally:
                harness.close()

    def test_extent_command_binds_byte_count_flush_and_exact_readback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inputs = Inputs(Path(temporary))
            try:
                extent = inputs.contract.extents[0]
                argv = importer.write_extent_argv(binding(), extent)
                self.assertEqual(argv[:2], ("/bin/sh", "-c"))
                script = argv[2]
                self.assertIn("wc -c", script)
                self.assertIn("/bin/sync", script)
                self.assertIn("blockdev --flushbufs", script)
                self.assertIn("FRANKENSARGO_DURANIUM_EXTENT_V1", script)
                self.assertIn(extent.sha256, argv)
                self.assertIn(str(extent.source_bytes), argv)
            finally:
                inputs.close()

    def test_bad_final_hash_cannot_publish_or_capture_vgcfg(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.bad_attestation = True
                with self.assertRaisesRegex(importer.ImportFailure, "attestation does not exactly match"):
                    harness.controller().execute()
                self.assertNotIn("publish", harness.remote.calls)
                self.assertNotIn("vgcfg", harness.remote.calls)
            finally:
                harness.close()

    def test_premature_publication_is_never_adopted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            harness = Harness(Path(temporary))
            try:
                harness.remote.state = dataclass_replace(
                    harness.remote.state,
                    disk_tags=importer.REQUIRED_PUBLISHED_TAGS,
                    disk_permission="r",
                )
                with self.assertRaisesRegex(importer.ImportFailure, "already published without"):
                    harness.controller().execute()
                self.assertNotIn("vgcfg", harness.remote.calls)
            finally:
                harness.close()


if __name__ == "__main__":
    unittest.main()
