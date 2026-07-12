#!/usr/bin/env python3

from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock
import uuid
import zlib


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import bootstrap_executor as executor  # noqa: E402
import bootstrap_plan  # noqa: E402
import pbread1  # noqa: E402


PLAN_PATH = ROOT / "examples/bootstrap-plan-v1.example.json"
DEVICE = "/dev/mmcblk0p72"
PARTUUID = "11111111-2222-4333-8444-555555555555"
SERIAL = "SYNTHETIC-SARGO"


def load_plan() -> dict[str, object]:
    return json.loads(PLAN_PATH.read_text())


def lvm_uuid(number: int) -> str:
    text = f"{number:032d}"
    return (
        f"{text[:6]}-{text[6:10]}-{text[10:14]}-{text[14:18]}-"
        f"{text[18:22]}-{text[22:26]}-{text[26:32]}"
    )


def lv_row(
    name: str,
    size: int,
    tags: list[str],
    number: int,
    *,
    segtype: str = "linear",
    devices: str = f"{DEVICE}(0)",
    chunk_size: int = 0,
    data_lv_uuid: str = "",
    metadata_lv_uuid: str = "",
    pool_lv_uuid: str = "",
    discards: str = "",
    lv_when_full: str = "",
    lv_attr: str = "-wc-------",
) -> dict[str, object]:
    return {
        "vg_uuid": lvm_uuid(900),
        "lv_uuid": lvm_uuid(number),
        "lv_name": name,
        "lv_size": str(size),
        "lv_active": 0,
        "lv_permissions": "unknown",
        "segtype": segtype,
        "seg_start_pe": "0",
        "seg_size_pe": str(size // (4 * 1024 * 1024)),
        "devices": [devices] if devices else [],
        "metadata_devices": [],
        "data_lv_uuid": data_lv_uuid,
        "metadata_lv_uuid": metadata_lv_uuid,
        "pool_lv_uuid": pool_lv_uuid,
        "lv_tags": list(tags),
        "lv_attr": lv_attr,
        "chunk_size": str(chunk_size),
        "discards": discards,
        "lv_when_full": lv_when_full,
    }


def snapshot_for_stage(stage: int, plan: dict[str, object] | None = None) -> executor.LvmSnapshot:
    plan = plan or load_plan()
    if stage == 0:
        return executor.LvmSnapshot((), (), ())
    pe_count = int(
        plan["lvm"]["capacity"]["conservative_extent_capacity_after_budget_bytes"]
    ) // (4 * 1024 * 1024)
    allocations = {1: 0, 2: 0, 3: 0, 4: 128, 5: 640, 6: 2688, 7: 2752, 8: 2880, 9: 8000, 10: 8128, 11: 8128}
    allocated = allocations[stage]
    pv_tags = [] if stage < 3 else ["greygoo.anchor", "pocketboot.pv.v1"]
    pv = {
        "pv_uuid": plan["lvm"]["pv"]["planned_uuid"],
        "pv_name": DEVICE,
        "dev_size": plan["partition"]["raw_bytes"],
        "pv_size": str(pe_count * 4 * 1024 * 1024),
        "pv_free": str((pe_count - allocated) * 4 * 1024 * 1024),
        "pe_start": str(32 * 1024 * 1024),
        "pv_mda_size": str(16 * 1024 * 1024 - 4096),
        "pv_mda_free": "1000000",
        "pv_mda_count": "2",
        "pv_mda_used_count": "0" if stage == 1 else "2",
        "pv_pe_count": str(pe_count),
        "pv_pe_alloc_count": str(allocated),
        "pv_tags": pv_tags,
        "vg_uuid": "" if stage == 1 else lvm_uuid(900),
        "vg_name": "" if stage == 1 else "franken",
    }
    if stage == 1:
        return executor.LvmSnapshot((pv,), (), ())
    volume_count = 0
    rows: list[dict[str, object]] = []
    volumes = plan["lvm"]["volumes"]
    command_volumes = [*volumes[:5], volumes[6]]
    if 4 <= stage <= 9:
        volume_count = stage - 3
        for number, volume in enumerate(command_volumes[:volume_count], start=1):
            rows.append(
                lv_row(
                    volume["name"],
                    int(volume["size_bytes"]),
                    volume["tags"],
                    number,
                )
            )
    elif stage >= 10:
        for number, volume in enumerate(volumes[:4], start=1):
            rows.append(
                lv_row(
                    volume["name"],
                    int(volume["size_bytes"]),
                    volume["tags"],
                    number,
                )
            )
        tdata_uuid = lvm_uuid(52)
        tmeta_uuid = lvm_uuid(53)
        pool_uuid = lvm_uuid(51)
        rows.extend(
            [
                lv_row(
                    "pool",
                    int(volumes[6]["size_bytes"]),
                    volumes[6]["tags"],
                    51,
                    segtype="thin-pool",
                    devices="",
                    chunk_size=int(plan["lvm"]["thin_pool"]["chunk_bytes"]),
                    data_lv_uuid=tdata_uuid,
                    metadata_lv_uuid=tmeta_uuid,
                    discards="nopassdown",
                    lv_when_full="error",
                    lv_attr="twi-------",
                ),
                lv_row(
                    "[pool_tdata]",
                    int(volumes[6]["size_bytes"]),
                    [],
                    52,
                    lv_attr="Twi-------",
                ),
                lv_row(
                    "[pool_tmeta]",
                    int(volumes[4]["size_bytes"]),
                    volumes[4]["tags"],
                    53,
                    lv_attr="ewi-------",
                ),
                lv_row(
                    "[lvol0_pmspare]",
                    int(volumes[5]["size_bytes"]),
                    [],
                    54,
                    lv_attr="ewi-------",
                ),
            ]
        )
        if stage == 11:
            rows.append(
                lv_row(
                    "disk-duranium",
                    int(volumes[7]["virtual_bytes"]),
                    volumes[7]["tags"],
                    61,
                    segtype="thin",
                    devices="",
                    pool_lv_uuid=pool_uuid,
                    lv_attr="Vwi-------",
                )
            )
        volume_count = len(rows)
    vg = {
        "vg_uuid": lvm_uuid(900),
        "vg_name": "franken",
        "vg_size": str(pe_count * 4 * 1024 * 1024),
        "vg_free": str((pe_count - allocated) * 4 * 1024 * 1024),
        "vg_extent_size": str(4 * 1024 * 1024),
        "vg_extent_count": str(pe_count),
        "vg_free_count": str(pe_count - allocated),
        "pv_count": "1",
        "lv_count": str(volume_count if stage < 10 else 5 + (1 if stage == 11 else 0)),
        "vg_missing_pv_count": "0",
        "vg_mda_count": "2",
        "vg_mda_used_count": "2",
        "vg_autoactivation": 0,
        "vg_tags": ["pocketboot.vg.v1"],
    }
    return executor.LvmSnapshot((pv,), (vg,), tuple(rows))


class FakeSourceVerifier:
    def __init__(self, digest: str) -> None:
        self.digest = digest
        self.calls = 0

    def verify(self, plan: dict[str, object], state_dir: Path) -> str:
        del plan, state_dir
        self.calls += 1
        return self.digest


class FakeTransport:
    def __init__(self, plan: dict[str, object], *, stage: int = 0) -> None:
        self.plan = plan
        self.serial = SERIAL
        self.stage = stage
        self.calls: list[tuple[str, ...]] = []
        self.files: dict[str, bytes] = {}
        self.fail_after_pvcreate = False
        self.fail_before_pvcreate = False
        self.fail_final_backup = False
        self.returncode_by_stage: dict[int, int] = {}
        self.vgcfg_returncode_by_stage: dict[int, int] = {}

    def connection_state(self) -> str:
        return "device"

    def reported_serial(self) -> str:
        return self.serial

    def run(self, argv: list[str], *, timeout: int = 120) -> executor.RemoteResult:
        del timeout
        command = tuple(argv)
        self.calls.append(command)
        if command[:2] == ("/bin/mkdir", "-p"):
            return executor.RemoteResult(command, 0, b"", b"")
        if len(command) >= 2 and command[0] == "/sbin/lvm.static":
            applet = command[1]
            if applet == "vgcfgbackup":
                path = command[command.index("--file") + 1]
                ids = executor.generated_ids(snapshot_for_stage(self.stage, self.plan))
                all_ids = [ids["pv_uuid"], ids["vg_uuid"], *ids["lv_uuids"].values()]
                body = "franken {\n" + "\n".join(
                    f'  id = "{identifier}"' for identifier in all_ids if identifier
                ) + "\n}\n"
                self.files[path] = body.encode()
                if self.fail_final_backup and path.endswith("/franken.vgcfg"):
                    return executor.RemoteResult(command, 5, b"", b"synthetic backup failure")
                return executor.RemoteResult(
                    command,
                    self.vgcfg_returncode_by_stage.get(self.stage, 0),
                    b"backup ok\n",
                    b"",
                )
            expected = self.plan["transaction"]["command_argv"][self.stage]["argv"]
            expected = executor.replace_placeholders(
                expected,
                device_path=DEVICE,
                state_dir=Path(self.state_dir),
                remote_dir=self.remote_dir,
            )
            if list(command) != expected:
                return executor.RemoteResult(command, 90, b"", b"unexpected argv")
            if self.stage == 0 and self.fail_before_pvcreate:
                raise executor.ExecuteError("synthetic disconnect before commit")
            self.stage = min(self.stage + 1, 11)
            if self.stage == 1 and self.fail_after_pvcreate:
                raise executor.ExecuteError("synthetic disconnect after commit")
            returncode = self.returncode_by_stage.get(self.stage, 0)
            return executor.RemoteResult(command, returncode, b"ok\n", b"")
        return executor.RemoteResult(command, 99, b"", b"unexpected command")

    def read_file(self, path: str, *, maximum: int = executor.MAX_REMOTE_FILE_BYTES) -> bytes:
        data = self.files[path]
        if len(data) > maximum:
            raise executor.ExecuteError("oversized fake")
        return data

    def list_dir(self, path: str) -> list[str]:
        del path
        return []

    def read_blocks(self, path: str, *, block_bytes: int, start: int, count: int) -> bytes:
        del path, block_bytes, start, count
        raise AssertionError("injected identity checker must avoid hardware reads")


class FakeLiveIdentityTransport:
    serial = SERIAL

    def __init__(self, plan: dict[str, object]) -> None:
        self.plan = plan
        self.file_overrides: dict[str, bytes] = {}
        self.run_overrides: dict[tuple[str, ...], executor.RemoteResult] = {}
        self.disk_sectors = 4096
        self.entry_count = 128
        self.entry_size = 128
        self.start = 100
        self.sectors = 3000
        entries = bytearray(self.entry_count * self.entry_size)
        offset = 71 * self.entry_size
        name = "userdata".encode("utf-16-le") + b"\x00\x00"
        name = name + b"\xaf" * (72 - len(name))
        entry = struct.pack(
            "<16s16sQQQ72s",
            uuid.UUID(plan["partition"]["type_guid"]).bytes_le,
            uuid.UUID(PARTUUID).bytes_le,
            self.start,
            self.start + self.sectors - 1,
            0,
            name,
        )
        entries[offset : offset + self.entry_size] = entry
        self.entries = bytes(entries)
        entry_crc = zlib.crc32(self.entries) & 0xFFFFFFFF

        def header(current: int, backup: int) -> bytes:
            raw = bytearray(512)
            struct.pack_into(
                "<8sIIIIQQQQ16sQIII",
                raw,
                0,
                b"EFI PART",
                0x00010000,
                92,
                0,
                0,
                current,
                backup,
                34,
                self.disk_sectors - 34,
                uuid.UUID(int=0).bytes_le,
                2,
                self.entry_count,
                self.entry_size,
                entry_crc,
            )
            struct.pack_into("<I", raw, 16, zlib.crc32(raw[:92]) & 0xFFFFFFFF)
            return bytes(raw)

        self.primary = header(1, self.disk_sectors - 1)
        self.backup = header(self.disk_sectors - 1, 1)
        plan["device"]["emmc_cid"] = "13" * 16
        plan["device"]["gpt_primary_header_sha256"] = executor.sha256_bytes(self.primary)
        plan["device"]["gpt_backup_header_sha256"] = executor.sha256_bytes(self.backup)
        plan["device"]["gpt_entry_array_sha256"] = executor.sha256_bytes(self.entries)
        plan["partition"]["start_lba"] = str(self.start)
        plan["partition"]["sectors"] = str(self.sectors)
        plan["partition"]["raw_bytes"] = str(self.sectors * 512)

    def connection_state(self) -> str:
        return "device"

    def reported_serial(self) -> str:
        return SERIAL

    def run(self, argv: list[str], *, timeout: int = 120) -> executor.RemoteResult:
        del timeout
        if tuple(argv) in self.run_overrides:
            return self.run_overrides[tuple(argv)]
        if argv == ["/usr/bin/id"]:
            return executor.RemoteResult(tuple(argv), 0, b"uid=0 gid=0\n", b"")
        if argv == ["/bin/readlink", "-f", "/sys/class/block/mmcblk0p72"]:
            return executor.RemoteResult(
                tuple(argv),
                0,
                b"/sys/devices/platform/synthetic/block/mmcblk0/mmcblk0p72\n",
                b"",
            )
        if argv == ["/bin/stat", "-L", "-c", "%f:%t:%T", DEVICE]:
            return executor.RemoteResult(tuple(argv), 0, b"61b0:103:48\n", b"")
        raise AssertionError(argv)

    def read_file(self, path: str, *, maximum: int = executor.MAX_REMOTE_FILE_BYTES) -> bytes:
        del maximum
        files = {
            "/proc/device-tree/compatible": b"google,sargo\x00qcom,sdm670\x00",
            "/sys/block/mmcblk0/device/cid": ("13" * 16 + "\n").encode(),
            "/sys/class/block/mmcblk0/queue/logical_block_size": b"512\n",
            "/sys/class/block/mmcblk0/size": f"{self.disk_sectors}\n".encode(),
            "/sys/class/block/mmcblk0/uevent": (
                "MAJOR=179\nMINOR=0\nDEVNAME=mmcblk0\nDEVTYPE=disk\n"
            ).encode(),
            "/sys/class/block/mmcblk0p72/uevent": (
                "MAJOR=259\nMINOR=72\nDEVNAME=mmcblk0p72\nDEVTYPE=partition\n"
                "PARTN=72\nPARTNAME=userdata\n"
                f"PARTUUID={PARTUUID}\n"
            ).encode(),
            "/sys/class/block/mmcblk0p72/start": f"{self.start}\n".encode(),
            "/sys/class/block/mmcblk0p72/size": f"{self.sectors}\n".encode(),
            "/sys/class/block/mmcblk0p72/dev": b"259:72\n",
            "/sys/class/block/mmcblk0p72/partition": b"72\n",
        }
        files.update(self.file_overrides)
        return files[path]

    def list_dir(self, path: str) -> list[str]:
        if path == "/sys/class/block":
            return ["mmcblk0", "mmcblk0p72"]
        raise AssertionError(path)

    def read_blocks(self, path: str, *, block_bytes: int, start: int, count: int) -> bytes:
        self.assert_read = (path, block_bytes)
        if start == 1 and count == 1:
            return self.primary
        if start == self.disk_sectors - 1 and count == 1:
            return self.backup
        if start == 2 and count == len(self.entries) // 512:
            return self.entries
        raise AssertionError((start, count))


def fake_dependencies(transport: FakeTransport, state_dir: Path) -> executor.ExecutorDependencies:
    transport.state_dir = str(state_dir)
    transport.remote_dir = f"{executor.REMOTE_ROOT}/{transport.plan['operation_uuid']}"
    identity = {
        "serial": SERIAL,
        "partuuid": PARTUUID,
        "kernel_name": "mmcblk0p72",
        "parent_kernel_name": "mmcblk0",
        "device_path": DEVICE,
        "major_minor": "259:72",
    }
    return executor.ExecutorDependencies(
        identity_checker=lambda remote, plan: dict(identity),
        device_binding_checker=lambda remote, live: {
            "device_path": DEVICE,
            "major_minor": "259:72",
        },
        quiescence_checker=lambda remote, live: {
            "mounted": False,
            "swap": False,
            "holders": [],
        },
        runtime_verifier=lambda remote, plan: {
            "lvm_static": {"sha256": plan["transaction"]["runtime_artifacts"]["lvm_static"]["sha256"]},
            "lvm_conf": {"sha256": plan["transaction"]["runtime_artifacts"]["lvm_conf"]["sha256"]},
        },
        snapshot_reader=lambda remote, plan, **kwargs: snapshot_for_stage(transport.stage, plan),
        backup_verifier=lambda plan, run, **kwargs: {
            "run_dir": str(run),
            "run_uuid": plan["artifacts"]["pbread1"]["run_uuid"],
            "manifest_sha256": plan["artifacts"]["pbread1"]["manifest_sha256"],
            "journal_sha256": plan["artifacts"]["pbread1"]["journal_sha256"],
            "raw_sha256": plan["artifacts"]["pbread1"]["raw_sha256"],
            "source_verified_at_utc": plan["artifacts"]["pbread1"]["source_verified_at_utc"],
            "status": "source-matched",
        },
    )


def make_executor(
    directory: Path,
    transport: FakeTransport,
    source: FakeSourceVerifier,
) -> executor.BootstrapExecutor:
    plan = transport.plan
    pbread_run = (directory / "pbread-run").resolve()
    pbread_run.mkdir(mode=0o700, parents=True, exist_ok=True)
    (pbread_run / ".lock").touch()
    (pbread_run / ".lock").chmod(0o600)
    return executor.BootstrapExecutor(
        plan=plan,
        plan_file_sha256="sha256:" + "ab" * 32,
        serial=SERIAL,
        partuuid=PARTUUID,
        confirmation=plan["confirmation"]["token"],
        recovery_attestation=executor.RECOVERY_ATTESTATION,
        pbread_run=pbread_run,
        state_dir=directory,
        transport=transport,
        source_verifier=source,
        dependencies=fake_dependencies(transport, directory),
        require_durable_state=False,
    )


class BootstrapExecutorTests(unittest.TestCase):
    def test_plan_loader_checks_schema_authorization_and_confirmation(self) -> None:
        plan, digest = executor.load_and_validate_plan(PLAN_PATH)
        self.assertEqual(plan["schema"], bootstrap_plan.PLAN_SCHEMA)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            changed = copy.deepcopy(plan)
            changed["partition"]["start_lba"] = "17359873"
            path.write_text(json.dumps(changed))
            with self.assertRaisesRegex(executor.ExecuteError, "authorization hash mismatch"):
                executor.load_and_validate_plan(path)

    def test_live_identity_gate_checks_gpt_sysfs_cid_geometry_and_name_prefix(self) -> None:
        plan = load_plan()
        transport = FakeLiveIdentityTransport(plan)
        identity = executor.check_live_identity_and_geometry(transport, plan)
        self.assertEqual(identity["device_path"], DEVICE)
        self.assertEqual(identity["partuuid"], PARTUUID)
        self.assertEqual(identity["gpt_entry_array_sha256"], plan["device"]["gpt_entry_array_sha256"])
        changed = copy.deepcopy(plan)
        changed["partition"]["start_lba"] = str(transport.start + 1)
        with self.assertRaisesRegex(executor.ExecuteError, "start LBA"):
            executor.check_live_identity_and_geometry(transport, changed)
        changed = copy.deepcopy(plan)
        changed["device"]["gpt_entry_array_sha256"] = "sha256:" + "00" * 32
        with self.assertRaisesRegex(executor.ExecuteError, "entry-array hash"):
            executor.check_live_identity_and_geometry(transport, changed)

    def test_live_identity_is_fixed_to_mmcblk0p72_parent_and_exact_block_rdev(self) -> None:
        plan = load_plan()
        wrong_plan = copy.deepcopy(plan)
        wrong_plan["partition"]["kernel_name_observation"] = "mmcblk1p72"
        with self.assertRaisesRegex(executor.ExecuteError, "fixed userdata node"):
            executor.check_live_identity_and_geometry(
                FakeLiveIdentityTransport(wrong_plan), wrong_plan
            )

        cases: list[tuple[str, callable]] = []

        def wrong_devtype(remote: FakeLiveIdentityTransport) -> None:
            remote.file_overrides["/sys/class/block/mmcblk0p72/uevent"] = (
                "MAJOR=259\nMINOR=72\nDEVNAME=mmcblk0p72\nDEVTYPE=disk\n"
                "PARTN=72\nPARTNAME=userdata\n"
                f"PARTUUID={PARTUUID}\n"
            ).encode()

        cases.append(("sysfs partition name", wrong_devtype))

        def wrong_parent(remote: FakeLiveIdentityTransport) -> None:
            argv = ("/bin/readlink", "-f", "/sys/class/block/mmcblk0p72")
            remote.run_overrides[argv] = executor.RemoteResult(
                argv,
                0,
                b"/sys/devices/platform/synthetic/block/mmcblk1/mmcblk0p72\n",
                b"",
            )

        cases.append(("parented by mmcblk0", wrong_parent))

        def regular_node(remote: FakeLiveIdentityTransport) -> None:
            argv = ("/bin/stat", "-L", "-c", "%f:%t:%T", DEVICE)
            remote.run_overrides[argv] = executor.RemoteResult(
                argv, 0, b"81a4:103:48\n", b""
            )

        cases.append(("not a block device", regular_node))

        def wrong_rdev(remote: FakeLiveIdentityTransport) -> None:
            argv = ("/bin/stat", "-L", "-c", "%f:%t:%T", DEVICE)
            remote.run_overrides[argv] = executor.RemoteResult(
                argv, 0, b"61b0:103:47\n", b""
            )

        cases.append(("rdev differs", wrong_rdev))
        for message, mutate in cases:
            with self.subTest(message=message):
                candidate_plan = load_plan()
                remote = FakeLiveIdentityTransport(candidate_plan)
                mutate(remote)
                with self.assertRaisesRegex(executor.ExecuteError, message):
                    executor.check_live_identity_and_geometry(remote, candidate_plan)

    def test_every_synthetic_stage_is_unique_and_exact(self) -> None:
        plan = load_plan()
        for stage in range(12):
            with self.subTest(stage=stage):
                snapshot = snapshot_for_stage(stage, plan)
                executor.validate_lvm_stage(snapshot, plan, stage, DEVICE)
                self.assertEqual(executor.detect_lvm_stage(snapshot, plan, DEVICE), stage)

    def test_production_report_reader_parses_json_std_and_derives_policy_columns(self) -> None:
        plan = load_plan()
        expected = snapshot_for_stage(10, plan)

        class ReportTransport:
            serial = SERIAL

            def __init__(self) -> None:
                self.lvs_argv: list[str] | None = None

            def run(self, argv: list[str], *, timeout: int = 120) -> executor.RemoteResult:
                del timeout
                applet = argv[1]
                section, rows = {
                    "pvs": ("pv", expected.pvs),
                    "vgs": ("vg", expected.vgs),
                    "lvs": ("lv", expected.lvs),
                }[applet]
                if applet == "lvs":
                    self.lvs_argv = argv
                payload = json.dumps({"report": [{section: list(rows)}]}).encode()
                return executor.RemoteResult(tuple(argv), 0, payload, b"")

        transport = ReportTransport()
        observed = executor.read_lvm_snapshot(
            transport,
            plan,
            device_path=DEVICE,
            state_dir=Path("/durable/state"),
            remote_dir="/run/frankensargo-bootstrap/01234567-89ab-4cde-8f01-23456789abcd",
        )
        self.assertEqual(executor.detect_lvm_stage(observed, plan, DEVICE), 10)
        self.assertIsNotNone(transport.lvs_argv)
        fields = transport.lvs_argv[transport.lvs_argv.index("-o") + 1]
        self.assertTrue(fields.endswith(",discards,lv_when_full"))

    def test_lvm_status_five_is_accepted_only_for_a_provably_empty_report(self) -> None:
        empty = executor.RemoteResult(
            ("lvm", "pvs"), 5, b'{"report":[{"pv":[]}]}', b"no rows"
        )
        self.assertEqual(executor._report_rows(empty, "pv"), [])
        nonempty = executor.RemoteResult(
            ("lvm", "pvs"),
            5,
            b'{"report":[{"pv":[{"pv_name":"/dev/mmcblk0p72"}]}]}',
            b"status mismatch",
        )
        with self.assertRaisesRegex(executor.ExecuteError, "status 5 with nonempty"):
            executor._report_rows(nonempty, "pv")

    def test_production_snapshot_reader_rejects_status_five_with_pv_rows(self) -> None:
        plan = load_plan()
        nonempty = snapshot_for_stage(1, plan)

        class StatusFiveReportTransport:
            serial = SERIAL

            def __init__(self) -> None:
                self.calls = 0

            def run(self, argv, *, timeout=120):
                del timeout
                self.calls += 1
                self.assert_argv = tuple(argv)
                payload = json.dumps(
                    {"report": [{"pv": list(nonempty.pvs)}]}
                ).encode()
                return executor.RemoteResult(tuple(argv), 5, payload, b"status 5")

        transport = StatusFiveReportTransport()
        with self.assertRaisesRegex(executor.ExecuteError, "status 5 with nonempty"):
            executor.read_lvm_snapshot(
                transport,
                plan,
                device_path=DEVICE,
                state_dir=Path("/durable/state"),
                remote_dir=(
                    "/run/frankensargo-bootstrap/"
                    "01234567-89ab-4cde-8f01-23456789abcd"
                ),
            )
        self.assertEqual(transport.calls, 3)

    def test_stage_model_rejects_wrong_pv_uuid_tags_placement_and_extra_lv(self) -> None:
        plan = load_plan()
        mutations = []
        wrong_uuid = copy.deepcopy(snapshot_for_stage(1, plan))
        wrong_uuid.pvs[0]["pv_uuid"] = lvm_uuid(999)
        mutations.append(wrong_uuid)
        wrong_tags = copy.deepcopy(snapshot_for_stage(9, plan))
        wrong_tags.lvs[0]["lv_tags"].append("surprise")
        mutations.append(wrong_tags)
        wrong_device = copy.deepcopy(snapshot_for_stage(10, plan))
        next(row for row in wrong_device.lvs if executor._lv_name(row["lv_name"]) == "pool_tmeta")["devices"] = ["/dev/mmcblk0p1(0)"]
        mutations.append(wrong_device)
        base = copy.deepcopy(snapshot_for_stage(11, plan))
        extra = executor.LvmSnapshot(base.pvs, base.vgs, base.lvs + (lv_row("intruder", 4 * 1024 * 1024, [], 99),))
        mutations.append(extra)
        for mutation in mutations:
            with self.subTest(index=mutations.index(mutation)):
                with self.assertRaises(executor.ExecuteError):
                    executor.detect_lvm_stage(mutation, plan, DEVICE)

    def test_stage_model_preserves_reserve_and_exact_thin_relationship_roles(self) -> None:
        plan = load_plan()
        too_small = copy.deepcopy(snapshot_for_stage(1, plan))
        planned = int(plan["lvm"]["capacity"]["planned_physical_lv_bytes"])
        reserve = int(plan["lvm"]["capacity"]["mandatory_recovery_reserve_bytes"])
        # Retaining exactly 16 GiB is still insufficient: the plan also binds
        # its conservative uncommitted slack beyond that reserve.
        too_small.pvs[0]["pv_size"] = str(planned + reserve)
        too_small.pvs[0]["pv_free"] = too_small.pvs[0]["pv_size"]
        mutations = [too_small]
        for field in ("data_lv_uuid", "metadata_lv_uuid"):
            broken = copy.deepcopy(snapshot_for_stage(10, plan))
            next(
                row for row in broken.lvs if executor._lv_name(row["lv_name"]) == "pool"
            )[field] = ""
            mutations.append(broken)
        missing_pool_link = copy.deepcopy(snapshot_for_stage(11, plan))
        next(
            row
            for row in missing_pool_link.lvs
            if executor._lv_name(row["lv_name"]) == "disk-duranium"
        )["pool_lv_uuid"] = ""
        mutations.append(missing_pool_link)
        wrong_role = copy.deepcopy(snapshot_for_stage(10, plan))
        next(
            row
            for row in wrong_role.lvs
            if executor._lv_name(row["lv_name"]) == "pool_tdata"
        )["lv_attr"] = "-wi-------"
        mutations.append(wrong_role)
        for index, mutation in enumerate(mutations):
            with self.subTest(index=index), self.assertRaises(executor.ExecuteError):
                executor.detect_lvm_stage(mutation, plan, DEVICE)

    def test_capacity_boundary_fields_fail_with_specific_authority(self) -> None:
        plan = load_plan()
        pe_bytes = int(plan["lvm"]["physical_extent_bytes"])
        conservative = int(
            plan["lvm"]["capacity"][
                "conservative_extent_capacity_after_budget_bytes"
            ]
        )

        pv_size = copy.deepcopy(snapshot_for_stage(1, plan))
        pv_size.pvs[0]["pv_size"] = str(conservative - pe_bytes)
        pv_size.pvs[0]["pv_free"] = str(conservative - pe_bytes)
        with self.assertRaisesRegex(
            executor.ExecuteError, "smaller than the plan's full conservative"
        ):
            executor.validate_lvm_stage(pv_size, plan, 1, DEVICE)

        pv_free = copy.deepcopy(snapshot_for_stage(1, plan))
        pv_free.pvs[0]["pv_free"] = str(
            int(pv_free.pvs[0]["pv_free"]) - pe_bytes
        )
        with self.assertRaisesRegex(
            executor.ExecuteError, "complete capacity as free"
        ):
            executor.validate_lvm_stage(pv_free, plan, 1, DEVICE)

        vg_free_count = copy.deepcopy(snapshot_for_stage(9, plan))
        vg_free_count.vgs[0]["vg_free_count"] = str(
            int(vg_free_count.vgs[0]["vg_free_count"]) - 1
        )
        with self.assertRaisesRegex(
            executor.ExecuteError, "VG free extent count differs"
        ):
            executor.validate_lvm_stage(vg_free_count, plan, 9, DEVICE)

        vg_free = copy.deepcopy(snapshot_for_stage(9, plan))
        vg_free.vgs[0]["vg_free"] = str(
            int(vg_free.vgs[0]["vg_free"]) - pe_bytes
        )
        with self.assertRaisesRegex(
            executor.ExecuteError, "VG free bytes differ"
        ):
            executor.validate_lvm_stage(vg_free, plan, 9, DEVICE)

        bad_slack_plan = copy.deepcopy(plan)
        capacity = bad_slack_plan["lvm"]["capacity"]
        capacity["uncommitted_slack_beyond_reserve_bytes"] = str(
            int(capacity["uncommitted_slack_beyond_reserve_bytes"]) - pe_bytes
        )
        with self.assertRaisesRegex(
            executor.ExecuteError, "does not preserve its recovery reserve arithmetic"
        ):
            executor.validate_lvm_stage(
                snapshot_for_stage(1, plan), bad_slack_plan, 1, DEVICE
            )

        final_free = copy.deepcopy(snapshot_for_stage(10, plan))
        allocated = int(final_free.pvs[0]["pv_pe_alloc_count"])
        reduced_pe_count = int(final_free.pvs[0]["pv_pe_count"]) - 1
        reduced_free_count = reduced_pe_count - allocated
        final_free.pvs[0]["pv_pe_count"] = str(reduced_pe_count)
        final_free.pvs[0]["pv_free"] = str(reduced_free_count * pe_bytes)
        final_free.vgs[0]["vg_extent_count"] = str(reduced_pe_count)
        final_free.vgs[0]["vg_free_count"] = str(reduced_free_count)
        final_free.vgs[0]["vg_free"] = str(reduced_free_count * pe_bytes)
        final_free.vgs[0]["vg_size"] = str(reduced_pe_count * pe_bytes)
        with self.assertRaisesRegex(
            executor.ExecuteError, "final layout lacks the plan's full conservative"
        ):
            executor.validate_lvm_stage(final_free, plan, 10, DEVICE)

    def test_full_fake_transaction_runs_exact_argv_and_resumes_without_replay(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            first = make_executor(state, transport, source).execute()
            self.assertTrue(first["bootstrap_complete"])
            self.assertEqual(first["stage"], 11)
            self.assertEqual(transport.stage, 11)
            self.assertEqual(source.calls, 1)
            for ordinal, command in enumerate(plan["transaction"]["command_argv"], start=1):
                checkpoint = state / "steps" / f"{ordinal:02d}-{command['step']}.json"
                self.assertTrue(checkpoint.is_file(), checkpoint)
            self.assertTrue((state / "franken.vgcfg").is_file())
            calls_before = list(transport.calls)
            second = make_executor(state, transport, source).execute()
            self.assertTrue(second["bootstrap_complete"])
            self.assertEqual(source.calls, 1, "source hash must not be compared after pvcreate")
            new_calls = transport.calls[len(calls_before) :]
            self.assertEqual(new_calls, [("/bin/mkdir", "-p", transport.remote_dir + "/steps")])

    def test_disconnect_after_pvcreate_without_remote_status_is_manual_forensics(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.fail_after_pvcreate = True
            with self.assertRaisesRegex(executor.ExecuteError, "synthetic disconnect"):
                make_executor(state, transport, source).execute()
            self.assertEqual(transport.stage, 1)
            self.assertFalse((state / "steps/01-create-anchor-pv.json").exists())
            transport.fail_after_pvcreate = False
            with self.assertRaisesRegex(
                executor.ExecuteError, "lacks a trustworthy remote status 0"
            ):
                make_executor(state, transport, source).execute(stop_after_step=1)
            self.assertFalse((state / "steps/01-create-anchor-pv.json").exists())
            pvcreate_calls = [call for call in transport.calls if len(call) > 1 and call[1] == "pvcreate"]
            self.assertEqual(len(pvcreate_calls), 1)

    def test_pvcreate_status_zero_can_recover_after_host_checkpoint_crash(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            run = state / "pbread-run"
            run.mkdir(mode=0o700)
            (run / ".lock").touch()
            (run / ".lock").chmod(0o600)
            transport = FakeTransport(plan)
            dependencies = fake_dependencies(transport, state)
            base_snapshot = dependencies.snapshot_reader
            crashed = False

            def crash_after_status(remote, plan_value, **kwargs):
                nonlocal crashed
                observed = base_snapshot(remote, plan_value, **kwargs)
                if transport.stage == 1 and not crashed:
                    crashed = True
                    raise executor.ExecuteError("synthetic host checkpoint crash")
                return observed

            dependencies.snapshot_reader = crash_after_status
            armed = executor.BootstrapExecutor(
                plan=plan,
                plan_file_sha256="sha256:" + "ab" * 32,
                serial=SERIAL,
                partuuid=PARTUUID,
                confirmation=plan["confirmation"]["token"],
                recovery_attestation=executor.RECOVERY_ATTESTATION,
                pbread_run=run,
                state_dir=state,
                transport=transport,
                source_verifier=source,
                dependencies=dependencies,
                require_durable_state=False,
            )
            with self.assertRaisesRegex(executor.ExecuteError, "checkpoint crash"):
                armed.execute(stop_after_step=1)
            self.assertFalse((state / "steps/01-create-anchor-pv.json").exists())
            result = make_executor(state, transport, source).execute(stop_after_step=1)
            self.assertEqual(result["stage"], 1)
            self.assertTrue((state / "steps/01-create-anchor-pv.json").is_file())
            pvcreate_calls = [
                call for call in transport.calls if len(call) > 1 and call[1] == "pvcreate"
            ]
            self.assertEqual(len(pvcreate_calls), 1)

    def test_committed_pvcreate_with_nonzero_remote_status_never_checkpoints(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.returncode_by_stage[1] = 1
            with self.assertRaisesRegex(
                executor.ExecuteError, "status 1; manual forensics required"
            ):
                make_executor(state, transport, source).execute(stop_after_step=1)
            self.assertEqual(transport.stage, 1)
            self.assertFalse((state / "steps/01-create-anchor-pv.json").exists())
            self.assertTrue(
                (state / "events/01-create-anchor-pv/post-nonzero-state.json").is_file()
            )
            with self.assertRaisesRegex(
                executor.ExecuteError, "durable nonzero remote status 1"
            ):
                make_executor(state, transport, source).execute(stop_after_step=1)
            pvcreate_calls = [
                call for call in transport.calls if len(call) > 1 and call[1] == "pvcreate"
            ]
            self.assertEqual(len(pvcreate_calls), 1)

    def test_later_main_commit_with_nonzero_status_refuses_restart_and_replay(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.returncode_by_stage[5] = 23
            with self.assertRaisesRegex(
                executor.ExecuteError, "status 23; manual forensics required"
            ):
                make_executor(state, transport, source).execute()
            self.assertEqual(transport.stage, 5)
            self.assertTrue((state / "steps/04-create-ggmeta.json").is_file())
            self.assertFalse((state / "steps/05-create-boot-rescue.json").exists())
            self.assertTrue(
                (state / "events/05-create-boot-rescue/post-nonzero-state.json").is_file()
            )
            with self.assertRaisesRegex(
                executor.ExecuteError, "durable nonzero remote status 23"
            ):
                make_executor(state, transport, source).execute()
            invocations = [
                call
                for call in transport.calls
                if "--name" in call
                and call[call.index("--name") + 1] == "boot-rescue"
            ]
            self.assertEqual(len(invocations), 1)
            self.assertFalse((state / "steps/05-create-boot-rescue.json").exists())

    def test_crash_after_durable_nonzero_result_still_refuses_on_restart(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.returncode_by_stage[4] = 19
            armed = make_executor(state, transport, source)
            base_snapshot = armed.dependencies.snapshot_reader
            crashed = False

            def crash_before_post_state(remote, plan_value, **kwargs):
                nonlocal crashed
                observed = base_snapshot(remote, plan_value, **kwargs)
                result_path = (
                    state
                    / "events/04-create-ggmeta/main-0001-result.json"
                )
                if transport.stage == 4 and result_path.is_file() and not crashed:
                    crashed = True
                    raise executor.ExecuteError(
                        "synthetic crash after durable nonzero result"
                    )
                return observed

            armed.dependencies.snapshot_reader = crash_before_post_state
            with self.assertRaisesRegex(
                executor.ExecuteError, "crash after durable nonzero result"
            ):
                armed.execute()
            self.assertEqual(transport.stage, 4)
            self.assertFalse((state / "steps/04-create-ggmeta.json").exists())
            self.assertFalse(
                (state / "events/04-create-ggmeta/post-nonzero-state.json").exists()
            )
            with self.assertRaisesRegex(
                executor.ExecuteError, "durable nonzero remote status 19"
            ):
                make_executor(state, transport, source).execute()
            invocations = [
                call
                for call in transport.calls
                if "--name" in call and call[call.index("--name") + 1] == "ggmeta"
            ]
            self.assertEqual(len(invocations), 1)
            self.assertFalse((state / "steps/04-create-ggmeta.json").exists())

    def test_stage_one_checkpoint_without_exact_pvcreate_intent_is_rejected(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            armed = make_executor(state, transport, source)
            armed.execute(stop_after_step=0)
            transport.stage = 1
            armed._write_checkpoint(
                plan["transaction"]["command_argv"][0],
                snapshot_for_stage(1, plan),
                None,
                recovered_postcondition=True,
            )
            with self.assertRaisesRegex(
                executor.ExecuteError, "no durable command-event|no durable main invocation"
            ):
                make_executor(state, transport, source).execute()

    def test_skeletal_or_corrupt_checkpoint_history_cannot_claim_completion(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            make_executor(state, transport, source).execute()
            final = state / "steps/12-backup-vg-metadata.json"
            executor.write_json(
                final,
                {
                    "authorization_sha256": plan["authorization_sha256"],
                    "ordinal": 12,
                    "stage": 11,
                },
            )
            with self.assertRaisesRegex(executor.ExecuteError, "checkpoint body/binding"):
                make_executor(state, transport, source).execute()

    def test_checkpoint_generated_ids_uuid_continuity_and_capture_hash_are_enforced(self) -> None:
        def completed(root: Path):
            plan = load_plan()
            source = FakeSourceVerifier(
                plan["partition"]["current_full_source_sha256"]
            )
            transport = FakeTransport(plan)
            make_executor(root, transport, source).execute()
            return plan, source, transport

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            plan, source, transport = completed(state)
            checkpoint_path = state / "steps/05-create-boot-rescue.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            checkpoint["generated_ids"] = {"pv_uuid": None, "vg_uuid": None, "lv_uuids": {}}
            executor.write_json(checkpoint_path, checkpoint)
            with self.assertRaisesRegex(executor.ExecuteError, "generated UUIDs"):
                make_executor(state, transport, source).execute()

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            plan, source, transport = completed(state)
            capture = state / "vgcfg/04-create-ggmeta.vgcfg"
            capture.write_bytes(capture.read_bytes() + b"# corrupt after checkpoint\n")
            with self.assertRaisesRegex(executor.ExecuteError, "file/evidence no longer match"):
                make_executor(state, transport, source).execute()

        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            plan, source, transport = completed(state)
            checkpoint_path = state / "steps/05-create-boot-rescue.json"
            checkpoint = json.loads(checkpoint_path.read_text())
            old_uuid = checkpoint["generated_ids"]["lv_uuids"]["ggmeta"]
            new_uuid = lvm_uuid(777)
            row = next(
                item
                for item in checkpoint["lvm_state"]["lvs"]
                if executor._lv_name(item["lv_name"]) == "ggmeta"
            )
            row["lv_uuid"] = new_uuid
            stored = executor.snapshot_from_canonical(
                checkpoint["lvm_state"], "mutated checkpoint"
            )
            checkpoint["lvm_state_sha256"] = stored.digest()
            checkpoint["generated_ids"] = executor.generated_ids(stored)
            capture_path = Path(checkpoint["vgcfgbackup"]["host_destination"])
            capture_data = capture_path.read_bytes().replace(
                f'id = "{old_uuid}"'.encode(), f'id = "{new_uuid}"'.encode()
            )
            capture_path.write_bytes(capture_data)
            evidence = executor._validate_vgcfg(capture_data, stored)
            checkpoint["vgcfgbackup"] = {
                **evidence,
                "remote_source": checkpoint["vgcfgbackup"]["remote_source"],
                "host_destination": str(capture_path),
            }
            executor.write_json(checkpoint_path, checkpoint)
            with self.assertRaisesRegex(executor.ExecuteError, "UUID continuity"):
                make_executor(state, transport, source).execute()

    def test_missing_first_write_outcome_invalidates_mutated_history(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            make_executor(state, transport, source).execute(stop_after_step=1)
            (state / "preflight/first-write-outcome.json").unlink()
            with self.assertRaisesRegex(executor.ExecuteError, "first-write outcome"):
                make_executor(state, transport, source).execute()

    def test_nonzero_final_vgcfgbackup_cannot_be_satisfied_by_stale_capture(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.fail_final_backup = True
            with self.assertRaisesRegex(
                executor.ExecuteError, "status 5; manual forensics required"
            ):
                make_executor(state, transport, source).execute()
            self.assertEqual(transport.stage, 11)
            self.assertFalse((state / "steps/12-backup-vg-metadata.json").exists())
            with self.assertRaisesRegex(
                executor.ExecuteError, "prior nonzero or corrupt remote result"
            ):
                make_executor(state, transport, source).execute()
            final_invocations = [
                call
                for call in transport.calls
                if len(call) > 1
                and call[1] == "vgcfgbackup"
                and "--file" in call
                and call[call.index("--file") + 1].endswith("/franken.vgcfg")
            ]
            self.assertEqual(len(final_invocations), 1)
            self.assertFalse((state / "steps/12-backup-vg-metadata.json").exists())

    def test_nonzero_intermediate_vgcfgbackup_never_allows_checkpoint_or_retry(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.vgcfg_returncode_by_stage[2] = 1
            with self.assertRaisesRegex(executor.ExecuteError, "step 2 vgcfgbackup failed"):
                make_executor(state, transport, source).execute()
            self.assertEqual(transport.stage, 2)
            self.assertFalse((state / "steps/02-create-anchor-vg.json").exists())
            with self.assertRaisesRegex(
                executor.ExecuteError, "prior nonzero or corrupt remote result"
            ):
                make_executor(state, transport, source).execute()
            captures = [
                call
                for call in transport.calls
                if len(call) > 1 and call[1] == "vgcfgbackup"
            ]
            self.assertEqual(len(captures), 1)

    def test_failed_or_unknown_pvcreate_is_never_replayed(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            transport.fail_before_pvcreate = True
            with self.assertRaisesRegex(executor.ExecuteError, "synthetic disconnect"):
                make_executor(state, transport, source).execute()
            transport.fail_before_pvcreate = False
            with self.assertRaisesRegex(executor.ExecuteError, "refusing to replay -ff"):
                make_executor(state, transport, source).execute()
            pvcreate_calls = [call for call in transport.calls if len(call) > 1 and call[1] == "pvcreate"]
            self.assertEqual(len(pvcreate_calls), 1)

    def test_live_source_mismatch_stops_before_any_lvm_command(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier("sha256:" + "ff" * 32)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            with self.assertRaisesRegex(executor.ExecuteError, "does not match backup-bound"):
                make_executor(state, transport, source).execute()
            self.assertFalse(any(len(call) > 1 and call[0] == "/sbin/lvm.static" for call in transport.calls))

    def test_stop_after_zero_persists_preflight_without_lvm_mutation(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            result = make_executor(state, transport, source).execute(stop_after_step=0)
            self.assertTrue(result["preflight_only"])
            self.assertEqual(result["stage"], 0)
            self.assertTrue((state / "preflight/prewrite.json").is_file())
            self.assertFalse((state / "steps").exists())
            self.assertFalse(any(len(call) > 1 and call[0] == "/sbin/lvm.static" for call in transport.calls))

    def test_one_pbread_shared_lock_spans_verify_source_intent_and_first_outcome(self) -> None:
        plan = load_plan()
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            run = state / "pbread-run"
            run.mkdir(mode=0o700)
            (run / ".lock").touch()
            (run / ".lock").chmod(0o600)
            checks: list[str] = []

            def assert_shared_lock(label: str) -> None:
                with self.assertRaises(pbread1.BackupError):
                    with pbread1.run_lock(run, exclusive=True):
                        pass
                checks.append(label)

            transport = FakeTransport(plan)
            original_run = transport.run

            def checking_run(argv, *, timeout=120):
                if len(argv) > 1 and argv[1] == "pvcreate":
                    assert_shared_lock("first-mutation")
                return original_run(argv, timeout=timeout)

            transport.run = checking_run
            dependencies = fake_dependencies(transport, state)
            base_backup = dependencies.backup_verifier

            def checking_backup(plan_value, run_value, **kwargs):
                self.assertTrue(kwargs.get("lock_held"))
                assert_shared_lock("host-backup")
                return base_backup(plan_value, run_value, **kwargs)

            dependencies.backup_verifier = checking_backup
            base_snapshot = dependencies.snapshot_reader

            def checking_snapshot(remote, plan_value, **kwargs):
                observed = base_snapshot(remote, plan_value, **kwargs)
                if (
                    transport.stage == 1
                    and (state / "preflight/first-write-outcome.json").exists()
                    and "first-outcome" not in checks
                ):
                    assert_shared_lock("first-outcome")
                return observed

            dependencies.snapshot_reader = checking_snapshot

            class CheckingSource(FakeSourceVerifier):
                def verify(self, plan_value, state_dir):
                    assert_shared_lock("live-source")
                    return super().verify(plan_value, state_dir)

            source = CheckingSource(plan["partition"]["current_full_source_sha256"])
            armed = executor.BootstrapExecutor(
                plan=plan,
                plan_file_sha256="sha256:" + "ab" * 32,
                serial=SERIAL,
                partuuid=PARTUUID,
                confirmation=plan["confirmation"]["token"],
                recovery_attestation=executor.RECOVERY_ATTESTATION,
                pbread_run=run,
                state_dir=state,
                transport=transport,
                source_verifier=source,
                dependencies=dependencies,
                require_durable_state=False,
            )
            armed.execute(stop_after_step=1)
            self.assertEqual(
                checks,
                ["host-backup", "live-source", "first-mutation", "first-outcome"],
            )

    def test_durable_directory_creation_fsyncs_each_new_entry_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            target = root / "one" / "two"
            calls: list[Path] = []
            real_fsync = executor._fsync_directory

            def recording(path: Path) -> None:
                calls.append(path)
                real_fsync(path)

            with mock.patch.object(executor, "_fsync_directory", side_effect=recording):
                executor.durable_mkdir(target)
            self.assertTrue(target.is_dir())
            self.assertIn(root, calls)
            self.assertIn(root / "one", calls)
            self.assertIn(target, calls)
            self.assertGreaterEqual(calls.count(root / "one"), 2)

    def test_pbread_run_on_volatile_filesystem_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run = Path(temporary).resolve() / "run"
            run.mkdir(mode=0o700)
            (run / ".lock").touch()
            (run / ".lock").chmod(0o600)
            with mock.patch.object(executor, "_mount_filesystem", return_value="tmpfs"):
                with self.assertRaisesRegex(executor.ExecuteError, "non-durable filesystem"):
                    executor.prepare_pbread_run_dir(run, require_durable=True)

    def test_host_backup_verifier_rehashes_complete_run_and_binds_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inventory_path = root / "inventory.json"
            image = root / "pocketboot.img"
            source = root / "userdata-source.raw"
            run = root / "pbread-run"
            sectors = 20
            raw_bytes = sectors * 512
            inventory: dict[str, object] = {
                "schema": "org.frankensargo.inventory/1",
                "device": {
                    "adb_serial": SERIAL,
                    "compatible": ["google,sargo"],
                    "emmc": {
                        "cid": "13" * 16,
                        "logical_sector_size": 512,
                        "sector_count": "4096",
                        "size_bytes": str(4096 * 512),
                    },
                    "product": "sargo",
                },
                "gpt": {
                    "backup_entry_array_independent": False,
                    "backup_entry_array_layout": "aliases-primary",
                    "disk_guid": "00000000-0000-0000-0000-000000000000",
                    "disk_guid_is_zero": True,
                    "entry_array_sha256": "sha256:" + "11" * 32,
                    "partitions": [
                        {
                            "byte_size": str(raw_bytes),
                            "kernel_node_observation": "mmcblk0p72",
                            "last_lba": str(100 + sectors - 1),
                            "name": "userdata",
                            "number": 72,
                            "partuuid": PARTUUID,
                            "sector_count": str(sectors),
                            "start_lba": "100",
                            "type_guid": bootstrap_plan.EXPECTED_PARTTYPE,
                        }
                    ],
                },
            }
            inventory["canonical_sha256"] = "sha256:" + pbread1.sha256_bytes(
                pbread1.canonical_json_bytes(inventory)
            )
            inventory_path.write_bytes(pbread1.pretty_json_bytes(inventory))
            image.write_bytes(b"synthetic pocketboot\n")
            source.write_bytes(bytes(range(256)) * (raw_bytes // 256))
            manifest = pbread1.manifest_from_inputs(
                inventory_path,
                SERIAL,
                PARTUUID,
                "userdata",
                image,
                4096,
                run_uuid="00000000-0000-4000-8000-000000000001",
                created_at="2026-07-12T00:00:00Z",
            )
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = pbread1.execute_backup(run, manifest, pbread1.OfflineTransport(source))
            stored_manifest, manifest_digest = pbread1.load_manifest(run)
            journal = json.loads((run / "journal.json").read_text())
            plan = load_plan()
            plan["device"]["fastboot_serial"] = SERIAL
            plan["device"]["emmc_cid"] = "13" * 16
            for field in (
                "partuuid",
                "type_guid",
                "partlabel",
                "kernel_name_observation",
                "start_lba",
                "sectors",
                "logical_sector_bytes",
                "raw_bytes",
            ):
                plan["partition"][field] = stored_manifest["partition"][field]
            plan["artifacts"]["pbread1"] = {
                "run_uuid": stored_manifest["run_uuid"],
                "manifest_sha256": f"sha256:{manifest_digest}",
                "journal_sha256": executor.sha256_bytes((run / "journal.json").read_bytes()),
                "source_verified_at_utc": journal["source_verification"]["verified_at_utc"],
                "status": "source-matched",
                "raw_sha256": f"sha256:{result.raw_sha256}",
            }
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                evidence = executor.verify_host_backup(plan, run)
            self.assertEqual(evidence["raw_sha256"], f"sha256:{result.raw_sha256}")
            raw = run / "userdata.raw"
            damaged = bytearray(raw.read_bytes())
            damaged[0] ^= 1
            raw.write_bytes(damaged)
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaisesRegex(executor.ExecuteError, "backup verification failed"):
                    executor.verify_host_backup(plan, run)

    def test_explicit_confirmation_serial_partuuid_and_attestation_are_required(self) -> None:
        plan = load_plan()
        source = FakeSourceVerifier(plan["partition"]["current_full_source_sha256"])
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary).resolve()
            transport = FakeTransport(plan)
            pbread_run = (state / "pbread-run").resolve()
            pbread_run.mkdir(mode=0o700)
            (pbread_run / ".lock").touch()
            (pbread_run / ".lock").chmod(0o600)
            base = dict(
                plan=plan,
                plan_file_sha256="sha256:" + "ab" * 32,
                serial=SERIAL,
                partuuid=PARTUUID,
                confirmation=plan["confirmation"]["token"],
                recovery_attestation=executor.RECOVERY_ATTESTATION,
                pbread_run=pbread_run,
                state_dir=state,
                transport=transport,
                source_verifier=source,
                dependencies=fake_dependencies(transport, state),
                require_durable_state=False,
            )
            for field, bad in (
                ("serial", "OTHER-SARGO"),
                ("partuuid", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"),
                ("confirmation", "NOPE"),
                ("recovery_attestation", "maybe"),
            ):
                arguments = {**base, field: bad}
                with self.subTest(field=field), self.assertRaises(executor.ExecuteError):
                    executor.BootstrapExecutor(**arguments)

    def test_quiescence_rejects_mount_swap_and_holder(self) -> None:
        class QuiescenceTransport:
            serial = SERIAL

            def __init__(self, mountinfo: bytes, swaps: bytes, holders: list[str]) -> None:
                self.mountinfo = mountinfo
                self.swaps = swaps
                self.holders = holders

            def read_file(self, path: str, *, maximum: int = executor.MAX_REMOTE_FILE_BYTES) -> bytes:
                del maximum
                return self.mountinfo if path.endswith("mountinfo") else self.swaps

            def list_dir(self, path: str) -> list[str]:
                del path
                return self.holders

        identity = {"kernel_name": "mmcblk0p72", "device_path": DEVICE, "major_minor": "259:72"}
        clean_mount = b"1 0 0:1 / / rw - rootfs rootfs rw\n"
        clean_swap = b"Filename\tType\tSize\tUsed\tPriority\n"
        executor.check_quiescence(QuiescenceTransport(clean_mount, clean_swap, []), identity)
        cases = (
            QuiescenceTransport(b"2 1 259:72 / /data rw - ext4 /dev/mmcblk0p72 rw\n", clean_swap, []),
            QuiescenceTransport(clean_mount, clean_swap + f"{DEVICE}\tpartition\t1\t0\t-2\n".encode(), []),
            QuiescenceTransport(clean_mount, clean_swap, ["dm-0"]),
        )
        for item in cases:
            with self.assertRaises(executor.ExecuteError):
                executor.check_quiescence(item, identity)

    def test_runtime_verifier_pulls_and_hashes_both_exact_files_and_version(self) -> None:
        plan = load_plan()
        altered = copy.deepcopy(plan)
        lvm_data = b"synthetic-static-lvm"
        conf_data = b"devices { scan = [] }\n"
        for name, data in (("lvm_static", lvm_data), ("lvm_conf", conf_data)):
            artifact = altered["transaction"]["runtime_artifacts"][name]
            artifact["bytes"] = str(len(data))
            artifact["sha256"] = "sha256:" + hashlib.sha256(data).hexdigest()

        class RuntimeTransport:
            serial = SERIAL

            def read_file(self, path: str, *, maximum: int = executor.MAX_REMOTE_FILE_BYTES) -> bytes:
                del maximum
                return lvm_data if path == "/sbin/lvm.static" else conf_data

            def run(self, argv: list[str], *, timeout: int = 120) -> executor.RemoteResult:
                del timeout
                return executor.RemoteResult(tuple(argv), 0, b"  LVM version:     2.03.35(2)\n", b"")

        observed = executor.verify_runtime_artifacts(RuntimeTransport(), altered)
        self.assertEqual(observed["lvm_static"]["bytes"], str(len(lvm_data)))
        bad = copy.deepcopy(altered)
        bad["transaction"]["runtime_artifacts"]["lvm_conf"]["sha256"] = "sha256:" + "00" * 32
        with self.assertRaisesRegex(executor.ExecuteError, "lvm_conf bytes/hash"):
            executor.verify_runtime_artifacts(RuntimeTransport(), bad)


if __name__ == "__main__":
    unittest.main()
