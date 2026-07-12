#!/usr/bin/env python3

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import jsonschema


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import bootstrap_plan as planner  # noqa: E402


OPERATION_UUID = "01234567-89ab-4cde-8f01-23456789abcd"
PV_UUID = "A1b2C3-d4E5-f6G7-h8J9-k0L1-m2N3-o4P5Q6"
PARTUUID = "11111111-2222-4333-8444-555555555555"


def evidence() -> planner.Evidence:
    return planner.Evidence(
        inventory_schema="org.frankensargo.inventory/1",
        inventory_file_sha256="01" * 32,
        inventory_canonical_sha256="02" * 32,
        pbread_manifest_sha256="03" * 32,
        pbread_journal_sha256="04" * 32,
        pbread_run_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        pbread_source_verified_at="2026-07-12T00:00:00Z",
        backup_raw_sha256="05" * 32,
        pocketboot_image_name="pocketboot-sargo-lab.img",
        pocketboot_image_bytes=8_000_000,
        pocketboot_image_sha256="06" * 32,
        fastboot_serial="SYNTHETIC-SARGO",
        emmc_cid="07" * 16,
        gpt_disk_guid="00000000-0000-0000-0000-000000000000",
        gpt_entry_array_sha256="08" * 32,
        gpt_primary_header_sha256="09" * 32,
        gpt_backup_header_sha256="0a" * 32,
        partuuid=PARTUUID,
        parttype=planner.EXPECTED_PARTTYPE,
        partlabel="userdata",
        kernel_name="mmcblk0p72",
        start_lba=17_359_872,
        sectors=104_782_815,
        sector_bytes=512,
        raw_bytes=planner.EXPECTED_RAW_BYTES,
    )


def built_plan() -> dict[str, object]:
    return planner.build_plan(evidence(), OPERATION_UUID, PV_UUID)


class PlanTests(unittest.TestCase):
    def test_example_is_exact_deterministic_plan_and_satisfies_schema(self) -> None:
        schema = json.loads((ROOT / "schema/bootstrap-plan-v1.schema.json").read_text())
        example = json.loads((ROOT / "examples/bootstrap-plan-v1.example.json").read_text())
        jsonschema.Draft202012Validator(schema).validate(example)
        self.assertEqual(built_plan(), example)
        self.assertEqual(built_plan(), built_plan())

    def test_authorization_hash_and_token_bind_the_complete_core(self) -> None:
        plan = built_plan()
        core = dict(plan)
        recorded = core.pop("authorization_sha256")
        confirmation = core.pop("confirmation")
        actual = "sha256:" + hashlib.sha256(planner.canonical_json_bytes(core)).hexdigest()
        self.assertEqual(recorded, actual)
        self.assertEqual(
            confirmation["token"],
            f"BOOTSTRAP-01234567-{actual.removeprefix('sha256:')[:12]}",
        )

    def test_schema_rejects_unsafe_argv_layout_and_allocation_mutations(self) -> None:
        schema = json.loads((ROOT / "schema/bootstrap-plan-v1.schema.json").read_text())
        validator = jsonschema.Draft202012Validator(schema)
        mutations = []

        shell_argv = copy.deepcopy(built_plan())
        shell_argv["transaction"]["command_argv"][0]["argv"] = [
            "sh",
            "-c",
            "wipe-everything",
        ]
        mutations.append(shell_argv)

        wrong_volume = copy.deepcopy(built_plan())
        wrong_volume["lvm"]["volumes"][0]["size_bytes"] = "1"
        mutations.append(wrong_volume)

        cross_kind_allocation = copy.deepcopy(built_plan())
        cross_kind_allocation["lvm"]["volumes"][0]["allocation"]["thin_pool"] = (
            "pool"
        )
        mutations.append(cross_kind_allocation)

        unfenced_report = copy.deepcopy(built_plan())
        del unfenced_report["transaction"]["verification_argv"][1][2:4]
        mutations.append(unfenced_report)

        false_checkpoint = copy.deepcopy(built_plan())
        false_checkpoint["transaction"]["command_argv"][0]["checkpoint"][
            "ordinal"
        ] = 2
        mutations.append(false_checkpoint)

        wrong_runtime_hash = copy.deepcopy(built_plan())
        wrong_runtime_hash["transaction"]["runtime_artifacts"]["lvm_static"][
            "sha256"
        ] = "sha256:" + "ff" * 32
        mutations.append(wrong_runtime_hash)

        wrong_runtime_version = copy.deepcopy(built_plan())
        wrong_runtime_version["transaction"]["runtime_artifacts"]["lvm_static"][
            "version"
        ] = "2.03.38"
        mutations.append(wrong_runtime_version)

        wrong_runtime_config = copy.deepcopy(built_plan())
        wrong_runtime_config["transaction"]["runtime_artifacts"]["lvm_conf"][
            "bytes"
        ] = "433"
        mutations.append(wrong_runtime_config)

        for mutation in mutations:
            with self.subTest(mutation=mutations.index(mutation)):
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(mutation)

    def test_runtime_artifacts_are_exact_prewrite_authority(self) -> None:
        transaction = built_plan()["transaction"]
        self.assertEqual(
            transaction["runtime_artifacts"],
            {
                "lvm_static": {
                    "path": "/sbin/lvm.static",
                    "version": "2.03.35",
                    "bytes": "2309032",
                    "sha256": (
                        "sha256:"
                        "b83d704df60ca281deb56f1704d74db731a05365e90d0162556b2c355b572d39"
                    ),
                },
                "lvm_conf": {
                    "path": "/etc/lvm/lvm.conf",
                    "bytes": "432",
                    "sha256": (
                        "sha256:"
                        "16eb1787836608cfaff40aa904705b2138928010b1b4011e4ab981b4d43e2998"
                    ),
                },
            },
        )
        self.assertIn(
            "pull-and-hash-runtime-files-and-match-lvm-version-before-first-write",
            transaction["preconditions"],
        )

    def test_capacity_is_exact_and_leaves_sixteen_gib_recovery_reserve(self) -> None:
        capacity = built_plan()["lvm"]["capacity"]
        self.assertEqual(capacity["partition_bytes"], "53648801280")
        self.assertEqual(capacity["planned_physical_lv_bytes"], "34091302912")
        self.assertEqual(
            capacity["conservative_extent_capacity_after_budget_bytes"],
            "53578039296",
        )
        self.assertEqual(capacity["extent_rounding_tail_bytes"], "3653120")
        self.assertEqual(
            capacity["planned_free_extents_after_allocations_bytes"],
            "19486736384",
        )
        self.assertEqual(capacity["mandatory_recovery_reserve_bytes"], "17179869184")
        self.assertEqual(capacity["uncommitted_slack_beyond_reserve_bytes"], "2306867200")
        total = (
            int(capacity["pv_metadata_alignment_budget_bytes"])
            + int(capacity["extent_rounding_tail_bytes"])
            + int(capacity["planned_physical_lv_bytes"])
            + int(capacity["mandatory_recovery_reserve_bytes"])
            + int(capacity["uncommitted_slack_beyond_reserve_bytes"])
        )
        self.assertEqual(total, planner.EXPECTED_RAW_BYTES)

    def test_critical_and_thin_metadata_extents_are_anchor_pinned(self) -> None:
        plan = built_plan()
        volumes = plan["lvm"]["volumes"]
        self.assertEqual(len(volumes), 8)
        for volume in volumes:
            if volume["critical"] or volume["kind"] in (
                "thin-metadata",
                "thin-metadata-spare",
            ):
                self.assertEqual(
                    volume["allocation"],
                    {"partuuid": PARTUUID, "policy": "exact-anchor-pv-only"},
                )
                if volume["kind"] != "thin-metadata-spare":
                    self.assertIn("greygoo.critical", volume["tags"])
        duranium = next(volume for volume in volumes if volume["name"] == "disk-duranium")
        self.assertEqual(duranium["virtual_bytes"], str(20 * 1024**3))
        self.assertEqual(duranium["allocation"], {"thin_pool": "pool", "policy": "thin-only"})
        self.assertNotIn("pocketboot.disk.v1", duranium["tags"])

    def test_command_argv_are_inert_exact_arrays_with_no_kernel_node_authority(self) -> None:
        plan = built_plan()
        commands = plan["transaction"]["command_argv"]
        self.assertEqual(len(commands), 12)
        self.assertEqual(
            [command["step"] for command in commands],
            [
                "create-anchor-pv",
                "create-anchor-vg",
                "tag-anchor-pv",
                "create-ggmeta",
                "create-boot-rescue",
                "create-home",
                "create-homed-state",
                "create-pool-meta",
                "create-pool",
                "convert-thin-pool",
                "create-duranium-thin-disk",
                "backup-vg-metadata",
            ],
        )
        self.assertEqual(commands[0]["argv"][:2], [planner.LVM_STATIC, "pvcreate"])
        self.assertEqual(commands[0]["argv"].count("--force"), 2)
        self.assertIn("--norestorefile", commands[0]["argv"])
        self.assertIn(PV_UUID, commands[0]["argv"])
        for command in commands[:10]:
            self.assertGreaterEqual(
                command["argv"].count(planner.DEVICE_PLACEHOLDER),
                2,
                msg=f"physical allocator is not anchor-pinned: {command['step']}",
            )
        for ordinal, command in enumerate(commands, start=1):
            self.assertEqual(
                command["argv"][:7],
                [
                    planner.LVM_STATIC,
                    command["argv"][1],
                    "--devices",
                    planner.DEVICE_PLACEHOLDER,
                    "--nohints",
                    "--config",
                    planner.LVM_CONFIG_OVERRIDE,
                ],
            )
            checkpoint = command["checkpoint"]
            self.assertEqual(checkpoint["ordinal"], ordinal)
            self.assertTrue(checkpoint["fsync_before_next_step"])
            self.assertEqual(
                checkpoint["resume_policy"],
                "accept-exact-postcondition-else-require-exact-precondition",
            )
            self.assertTrue(checkpoint["expected_before"])
            self.assertTrue(checkpoint["expected_after"])
            self.assertTrue(checkpoint["capture_fields"])
            if 2 <= ordinal <= 11:
                self.assertIsNotNone(checkpoint["vgcfgbackup_argv"])
                self.assertIsNotNone(checkpoint["remote_capture"])
                self.assertIn("--readonly", checkpoint["vgcfgbackup_argv"])
            elif ordinal == 12:
                self.assertIsNone(checkpoint["vgcfgbackup_argv"])
                self.assertIsNotNone(checkpoint["remote_capture"])
            else:
                self.assertIsNone(checkpoint["vgcfgbackup_argv"])
                self.assertIsNone(checkpoint["remote_capture"])
        conversion = commands[9]["argv"]
        self.assertEqual(
            conversion[conversion.index("--poolmetadataspare") + 1], "y"
        )
        self.assertEqual(commands[-1]["argv"][1], "vgcfgbackup")
        extra_argv = [
            plan["transaction"]["post_import_argv"][0]["argv"],
            *plan["transaction"]["verification_argv"],
        ]
        for argv in extra_argv:
            self.assertEqual(
                argv[:7],
                [
                    planner.LVM_STATIC,
                    argv[1],
                    "--devices",
                    planner.DEVICE_PLACEHOLDER,
                    "--nohints",
                    "--config",
                    planner.LVM_CONFIG_OVERRIDE,
                ],
            )
        for argv in plan["transaction"]["verification_argv"]:
            self.assertIn("--readonly", argv)
            self.assertIn("--nolocking", argv)
        post_checkpoint = plan["transaction"]["post_import_argv"][0]["checkpoint"]
        self.assertEqual(post_checkpoint["ordinal"], 13)
        self.assertIn("--readonly", post_checkpoint["vgcfgbackup_argv"])
        encoded = json.dumps(commands)
        self.assertNotIn("mmcblk0p72", encoded)
        self.assertIn(planner.DEVICE_PLACEHOLDER, encoded)
        source = (ROOT / "lib/bootstrap_plan.py").read_text()
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("os.system", source)

    def test_wrong_target_geometry_and_uuid_policy_are_rejected(self) -> None:
        mutations = [
            ("partlabel", "system_a", "not the userdata"),
            ("parttype", "97d7b011-54da-4835-b3c4-917ad6e73d74", "GPT type"),
            ("sector_bytes", 4096, "sector size"),
            ("raw_bytes", planner.EXPECTED_RAW_BYTES - 512, "geometry"),
            ("gpt_disk_guid", PARTUUID, "zero GUID"),
        ]
        for field, value, diagnostic in mutations:
            with self.subTest(field=field):
                changed = dataclass_replace(evidence(), **{field: value})
                with self.assertRaisesRegex(planner.PlanError, diagnostic):
                    planner.build_plan(changed, OPERATION_UUID, PV_UUID)
        with self.assertRaisesRegex(planner.PlanError, "version-4"):
            planner.build_plan(
                evidence(),
                "01234567-89ab-3cde-8f01-23456789abcd",
                PV_UUID,
            )
        with self.assertRaisesRegex(planner.PlanError, "LVM UUID"):
            planner.build_plan(evidence(), OPERATION_UUID, "not-an-lvm-uuid")


def dataclass_replace(value: planner.Evidence, **changes: object) -> planner.Evidence:
    values = {field.name: getattr(value, field.name) for field in planner.dataclasses.fields(value)}
    values.update(changes)
    return planner.Evidence(**values)


class EvidenceLoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="frankensargo-bootstrap-")
        self.root = Path(self.temporary.name)
        self.inventory_path = self.root / "inventory.json"
        self.inventory_path.write_text("synthetic inventory\n")
        self.run = self.root / "pbread"
        self.run.mkdir()
        (self.run / ".lock").write_bytes(b"")
        (self.run / "journal.json").write_text("synthetic journal\n")
        self.image = self.root / "pocketboot.img"
        self.image.write_bytes(b"synthetic pocketboot\n")
        self.image_sha = planner.sha256_file(self.image)
        self.inventory = {
            "schema": "org.frankensargo.inventory/1",
            "canonical_sha256": "sha256:" + "11" * 32,
            "device": {
                "product": "sargo",
                "compatible": ["google,sargo", "qcom,sdm670"],
                "adb_serial": "SYNTHETIC-SARGO",
                "emmc": {
                    "cid": "22" * 16,
                    "logical_sector_size": 512,
                },
            },
            "gpt": {
                "disk_guid": "00000000-0000-0000-0000-000000000000",
                "disk_guid_is_zero": True,
                "backup_entry_array_layout": "aliases-primary",
                "entry_array_sha256": "sha256:" + "33" * 32,
                "primary_header": {"sector_sha256": "sha256:" + "44" * 32},
                "backup_header": {"sector_sha256": "sha256:" + "55" * 32},
                "partitions": [
                    {
                        "partuuid": PARTUUID,
                        "type_guid": planner.EXPECTED_PARTTYPE,
                        "name": "userdata",
                        "kernel_node_observation": "mmcblk0p72",
                        "start_lba": "17359872",
                        "sector_count": "104782815",
                        "byte_size": str(planner.EXPECTED_RAW_BYTES),
                    }
                ],
            },
        }
        self.pb_manifest = {
            "run_uuid": "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
            "device": {
                "fastboot_serial": "SYNTHETIC-SARGO",
                "emmc_cid": "22" * 16,
                "gpt_disk_guid": "00000000-0000-0000-0000-000000000000",
            },
            "inventory": {
                "canonical_sha256": "sha256:" + "11" * 32,
                "entry_array_sha256": "sha256:" + "33" * 32,
            },
            "partition": {
                "partuuid": PARTUUID,
                "type_guid": planner.EXPECTED_PARTTYPE,
                "partlabel": "userdata",
                "kernel_name_observation": "mmcblk0p72",
                "start_lba": "17359872",
                "sectors": "104782815",
                "logical_sector_bytes": 512,
                "raw_bytes": str(planner.EXPECTED_RAW_BYTES),
            },
            "pocketboot": {
                "image_sha256": "sha256:" + self.image_sha,
                "image_bytes": str(self.image.stat().st_size),
            },
        }
        self.raw_sha = "66" * 32
        self.journal = {
            "source_verification": {
                "status": "matched",
                "source_sha256": "sha256:" + self.raw_sha,
                "verified_at_utc": "2026-07-12T00:00:00Z",
            },
            "assembled": {"sha256": "sha256:" + self.raw_sha},
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def load(self) -> planner.Evidence:
        with (
            mock.patch.object(planner.pbread1, "verified_inventory", return_value=self.inventory),
            mock.patch.object(planner.pbread1, "verify_run", return_value=self.raw_sha) as verify,
            mock.patch.object(
                planner.pbread1,
                "load_manifest",
                return_value=(self.pb_manifest, "77" * 32),
            ),
            mock.patch.object(planner.pbread1, "load_journal", return_value=self.journal),
        ):
            result = planner.load_evidence(
                self.inventory_path,
                self.run,
                self.image,
                "SYNTHETIC-SARGO",
                PARTUUID,
            )
            verify.assert_called_once_with(self.run.resolve())
            return result

    def test_completed_pbread_manifest_and_journal_are_cross_bound(self) -> None:
        loaded = self.load()
        self.assertEqual(loaded.backup_raw_sha256, self.raw_sha)
        self.assertEqual(loaded.pocketboot_image_sha256, self.image_sha)
        self.assertEqual(loaded.inventory_canonical_sha256, "11" * 32)
        self.assertEqual(loaded.gpt_entry_array_sha256, "33" * 32)

    def test_identity_and_hash_mismatches_are_rejected(self) -> None:
        cases = [
            ("serial", lambda: self.pb_manifest["device"].__setitem__("fastboot_serial", "OTHER")),
            ("CID", lambda: self.pb_manifest["device"].__setitem__("emmc_cid", "ff" * 16)),
            (
                "inventory hash",
                lambda: self.pb_manifest["inventory"].__setitem__(
                    "canonical_sha256", "sha256:" + "ff" * 32
                ),
            ),
            (
                "GPT entry-array hash",
                lambda: self.pb_manifest["inventory"].__setitem__(
                    "entry_array_sha256", "sha256:" + "ff" * 32
                ),
            ),
            (
                "partition start_lba",
                lambda: self.pb_manifest["partition"].__setitem__("start_lba", "1"),
            ),
            (
                "PocketBoot image",
                lambda: self.pb_manifest["pocketboot"].__setitem__(
                    "image_sha256", "sha256:" + "ff" * 32
                ),
            ),
        ]
        for diagnostic, mutate in cases:
            with self.subTest(diagnostic=diagnostic):
                original = copy.deepcopy(self.pb_manifest)
                mutate()
                with self.assertRaisesRegex(planner.PlanError, diagnostic):
                    self.load()
                self.pb_manifest = original


if __name__ == "__main__":
    unittest.main()
