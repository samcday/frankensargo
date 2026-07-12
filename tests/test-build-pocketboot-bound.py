#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import fcntl
import json
import runpy
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "bin" / "build-pocketboot-bound"
MODULE = runpy.run_path(str(TOOL), run_name="frankensargo_bound_builder_test")
BuildError = MODULE["BuildError"]
PARTUUID = "db04e713-11c3-4d68-bec2-8cc483bd3891"
VG_UUID = "BBBBBB-1111-2222-3333-4444-5555-CCCCCC"
BASE_CMDLINE = "pocketboot.log=debug pocketboot.acm sysrq_always_enabled=1"
NO_ACM_CMDLINE = "pocketboot.log=debug sysrq_always_enabled=1"


class BoundPocketBootTests(unittest.TestCase):
    def test_renders_one_exact_binding_without_mutating_other_toml(self):
        original = (
            'name = "Google Pixel 3a (sargo)"\n'
            '[bootimg]\n'
            f"cmdline = {json.dumps(BASE_CMDLINE)}\n"
            'bootimg_version = 2\n'
        ).encode()
        rendered, base, profile, bound = MODULE["render_bound_config"](
            original, VG_UUID, [PARTUUID]
        )
        self.assertEqual(base, BASE_CMDLINE)
        self.assertEqual(profile, BASE_CMDLINE)
        self.assertEqual(
            bound,
            BASE_CMDLINE
            + f" pocketboot.vg_uuid={VG_UUID} pocketboot.pv_partuuid={PARTUUID}",
        )
        self.assertIn(f"cmdline = {json.dumps(bound)}\n".encode(), rendered)
        self.assertTrue(rendered.endswith(b'bootimg_version = 2\n'))
        self.assertEqual(original.count(b"cmdline = "), 1)

    def test_composes_no_acm_with_binding_in_real_nested_bootimg_table(self):
        original = (
            'name = "Google Pixel 3a (sargo)"\n\n'
            '[bootimg]\n'
            f"cmdline = {json.dumps(BASE_CMDLINE)}\n"
            'header_version = 2\n'
        ).encode()
        rendered, base, profile, bound = MODULE["render_bound_config"](
            original, VG_UUID, [PARTUUID], no_acm=True
        )
        self.assertEqual(base, BASE_CMDLINE)
        self.assertEqual(profile, NO_ACM_CMDLINE)
        self.assertNotIn("pocketboot.acm", bound)
        self.assertEqual(
            bound,
            profile
            + f" pocketboot.vg_uuid={VG_UUID} pocketboot.pv_partuuid={PARTUUID}",
        )
        self.assertIn(f"cmdline = {json.dumps(bound)}\n".encode(), rendered)

    def test_rejects_top_level_only_cmdline_fixture(self):
        original = f"cmdline = {json.dumps(BASE_CMDLINE)}\n".encode()
        with self.assertRaisesRegex(BuildError, "forbidden top-level"):
            MODULE["render_bound_config"](original, VG_UUID, [PARTUUID])

    def test_rejects_existing_binding_duplicate_partuuid_and_bad_sysrq_policy(self):
        with self.assertRaisesRegex(BuildError, "already contains an LVM binding"):
            MODULE["bound_cmdline"](
                BASE_CMDLINE + " pocketboot.vg_uuid=" + VG_UUID,
                VG_UUID,
                [PARTUUID],
            )
        with self.assertRaisesRegex(BuildError, "exactly one sysrq"):
            MODULE["bound_cmdline"]("pocketboot.acm", VG_UUID, [PARTUUID])
        with self.assertRaisesRegex(BuildError, "canonical lowercase"):
            MODULE["checked_partuuid"](PARTUUID.upper())

    def test_extracts_complete_android_v2_cmdline_and_rejects_padding(self):
        cmdline = BASE_CMDLINE + " " + "x" * 700
        raw = cmdline.encode() + b"\0"
        raw += b"\0" * (1536 - len(raw))
        image = bytearray(1632)
        image[:8] = b"ANDROID!"
        image[40:44] = (2).to_bytes(4, "little")
        image[64:576] = raw[:512]
        image[608:1632] = raw[512:]
        self.assertEqual(MODULE["android_boot_cmdline"](bytes(image)), cmdline)

        image[-1] = 1
        with self.assertRaisesRegex(BuildError, "noncanonical padding"):
            MODULE["android_boot_cmdline"](bytes(image))

    def test_interrupted_edit_journal_restores_only_known_bound_side(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "sargo.toml"
            journal = root / "journal.json"
            original = b'cmdline = "base"\n'
            bound = b'cmdline = "bound"\n'
            config.write_bytes(bound)
            MODULE["write_journal"](
                journal, original, bound, "base-tree", "bound cmdline"
            )
            MODULE["recover_interrupted_edit"](config, journal)
            self.assertEqual(config.read_bytes(), original)
            self.assertFalse(journal.exists())

            config.write_bytes(b"unrelated user edit\n")
            MODULE["write_journal"](
                journal, original, bound, "base-tree", "bound cmdline"
            )
            with self.assertRaisesRegex(BuildError, "differs from both sides"):
                MODULE["recover_interrupted_edit"](config, journal)
            self.assertEqual(config.read_bytes(), b"unrelated user edit\n")

    def test_provenance_records_binding_trees_patches_and_output(self):
        sources = MODULE["load_sources"]()
        data = MODULE["provenance"](
            sources=sources,
            image_sha="a" * 64,
            image_bytes=1234,
            base_tree="b" * 40,
            bound_tree="c" * 40,
            base_cmdline=BASE_CMDLINE,
            profile_cmdline=NO_ACM_CMDLINE,
            bound_cmdline_value=NO_ACM_CMDLINE + " bound",
            no_acm=True,
            output_name="pocketboot-sargo-lvm-bound-noacm.img",
            vg_uuid=VG_UUID,
            partuuids=[PARTUUID],
            patches=MODULE["patch_records"](),
        )
        value = json.loads(data)
        self.assertEqual(value["format"], "org.frankensargo.pocketboot-bound-image/1")
        self.assertEqual(value["binding"]["vg_uuid"], VG_UUID)
        self.assertEqual(value["binding"]["pv_partuuids"], [PARTUUID])
        self.assertEqual(value["pocketboot"]["base_patched_tree"], "b" * 40)
        self.assertEqual(value["pocketboot"]["bound_tree"], "c" * 40)
        self.assertEqual(
            value["pocketboot"]["profile_source_delta"],
            "remove-cmdline-token:pocketboot.acm",
        )
        self.assertEqual(value["profile"], "interim-lvm-bound-lab-no-acm")
        self.assertEqual(
            value["output"]["basename"],
            "pocketboot-sargo-lvm-bound-noacm.img",
        )
        self.assertEqual(value["output"]["sha256"], "a" * 64)
        self.assertTrue(value["patches"])
        for patch in value["patches"]:
            path = ROOT / "patches" / "pocketboot" / patch["name"]
            self.assertEqual(
                patch["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )

    def test_common_source_lock_rejects_a_second_builder(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            lock_path = Path(f"{source}.frankensargo-build.lock")
            first = lock_path.open("a+b")
            self.addCleanup(first.close)
            fcntl.flock(first.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(BuildError, "another PocketBoot build"):
                MODULE["acquire_source_lock"](source)


if __name__ == "__main__":
    unittest.main()
