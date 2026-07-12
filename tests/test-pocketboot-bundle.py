#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pocketboot_bundle as bundle  # noqa: E402
import pocketboot_profile as image_profile  # noqa: E402


IMAGE_NAME = "pocketboot-sargo-lab.img"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def temporary_members(root: Path, profile: str) -> tuple[dict[str, Path], bytes]:
    image = b"ANDROID!" + bytes(range(256)) * 16
    image_hash = sha(image)
    paths = {
        "image": root / ".image.tmp",
        "sha256": root / ".sha256.tmp",
        "provenance": root / ".provenance.tmp",
    }
    paths["image"].write_bytes(image)
    paths["sha256"].write_bytes(f"{image_hash}  {IMAGE_NAME}\n".encode())
    provenance = [f"profile={profile}", f"sha256={image_hash}"]
    if profile == "lab-no-acm":
        profile_value = {
            "format": image_profile.METADATA_FORMAT,
            "profile": profile,
            "application": "android-v2-header-postprocess",
            "base_cmdline": (
                "pocketboot.log=debug pocketboot.acm sysrq_always_enabled=1"
            ),
            "effective_cmdline": "pocketboot.log=debug sysrq_always_enabled=1",
            "parent_sha256": sha(image + b"parent"),
            "result_sha256": image_hash,
            "bytes": len(image),
            "allowed_ranges": [list(item) for item in image_profile.CMDLINE_RANGES],
            "changed_spans": [[85, 122]],
            "all_other_bytes_unchanged": True,
            "unsigned_unsealed_assertion": "android-v2-without-avb-footer",
        }
        profile_data = (json.dumps(profile_value, sort_keys=True) + "\n").encode()
        paths["profile"] = root / ".profile.tmp"
        paths["profile"].write_bytes(profile_data)
        provenance.extend(
            (
                f"profile_application={profile_value['application']}",
                "profile_source_delta=none",
                f"profile_base_cmdline={profile_value['base_cmdline']}",
                f"profile_effective_cmdline={profile_value['effective_cmdline']}",
                f"profile_parent_sha256={profile_value['parent_sha256']}",
                f"profile_result_sha256={profile_value['result_sha256']}",
                "profile_allowed_ranges="
                + json.dumps(profile_value["allowed_ranges"], separators=(",", ":")),
                "profile_changed_spans="
                + json.dumps(profile_value["changed_spans"], separators=(",", ":")),
                "profile_all_other_bytes_unchanged=true",
                "profile_unsigned_unsealed_assertion="
                + str(profile_value["unsigned_unsealed_assertion"]),
            )
        )
        provenance.append(
            f"profile_metadata={IMAGE_NAME}.profile.json sha256={sha(profile_data)}"
        )
    paths["provenance"].write_bytes(("\n".join(provenance) + "\n").encode())
    return paths, image


class PocketBootBundleTests(unittest.TestCase):
    def test_manifest_last_bundle_publishes_and_verifies_both_profiles(self) -> None:
        for profile in ("lab", "lab-no-acm"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                members, image = temporary_members(root, profile)
                manifest = bundle.publish_bundle(
                    output_dir=root,
                    image_name=IMAGE_NAME,
                    profile=profile,
                    temporary_members=members,
                )
                value = bundle.verify_bundle(manifest)
                self.assertEqual(value["profile"], profile)
                self.assertEqual((root / IMAGE_NAME).read_bytes(), image)
                self.assertFalse(any(path.exists() for path in members.values()))
                self.assertTrue(manifest.is_file())

    def test_existing_destination_is_refused_without_changing_stale_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stale = root / IMAGE_NAME
            stale.write_bytes(b"old image")
            before = stale.read_bytes()
            with self.assertRaisesRegex(bundle.BundleError, "refusing existing"):
                bundle.check_destination(root, IMAGE_NAME, "lab")
            self.assertEqual(stale.read_bytes(), before)
            with self.assertRaisesRegex(bundle.BundleError, "cannot open"):
                bundle.verify_bundle(root / f"{IMAGE_NAME}.bundle.json")

    def test_simulated_hard_kill_leaves_no_completion_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members, _image = temporary_members(root, "lab")
            with self.assertRaises(bundle.SimulatedHardCrash):
                bundle.publish_bundle(
                    output_dir=root,
                    image_name=IMAGE_NAME,
                    profile="lab",
                    temporary_members=members,
                    _simulate_hard_crash_after_members=1,
                )
            self.assertTrue((root / IMAGE_NAME).exists())
            marker = root / f"{IMAGE_NAME}.bundle.json"
            self.assertFalse(marker.exists())
            with self.assertRaisesRegex(bundle.BundleError, "cannot open"):
                bundle.verify_bundle(marker)

    def test_ordinary_mid_publication_failure_rolls_back_final_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members, _image = temporary_members(root, "lab")
            real_link = os.link
            calls = 0

            def failing_link(source, destination, *, follow_symlinks=True):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("synthetic link failure")
                return real_link(source, destination, follow_symlinks=follow_symlinks)

            with mock.patch.object(bundle.os, "link", side_effect=failing_link):
                with self.assertRaisesRegex(OSError, "synthetic link failure"):
                    bundle.publish_bundle(
                        output_dir=root,
                        image_name=IMAGE_NAME,
                        profile="lab",
                        temporary_members=members,
                    )
            names = bundle.bundle_names(IMAGE_NAME, "lab")
            self.assertFalse(any((root / name).exists() for name in names.values()))

    def test_manifest_rejects_member_changed_after_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members, _image = temporary_members(root, "lab")
            manifest = bundle.publish_bundle(
                output_dir=root,
                image_name=IMAGE_NAME,
                profile="lab",
                temporary_members=members,
            )
            (root / IMAGE_NAME).write_bytes(b"replacement")
            with self.assertRaisesRegex(bundle.BundleError, "differs from the completion"):
                bundle.verify_bundle(manifest)

    def test_verifier_rejects_symlinked_manifest_or_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members, _image = temporary_members(root, "lab")
            manifest = bundle.publish_bundle(
                output_dir=root,
                image_name=IMAGE_NAME,
                profile="lab",
                temporary_members=members,
            )
            alias = root / "manifest-alias.json"
            alias.symlink_to(manifest.name)
            with self.assertRaisesRegex(bundle.BundleError, "cannot open"):
                bundle.verify_bundle(alias)

            image = root / IMAGE_NAME
            saved = root / "saved-image"
            image.rename(saved)
            image.symlink_to(saved.name)
            with self.assertRaisesRegex(bundle.BundleError, "cannot open"):
                bundle.verify_bundle(manifest)

    def test_incomplete_no_acm_profile_cannot_publish_even_with_matching_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            members, image = temporary_members(root, "lab-no-acm")
            incomplete = {
                "format": image_profile.METADATA_FORMAT,
                "profile": "lab-no-acm",
                "bytes": len(image),
                "result_sha256": sha(image),
            }
            profile_data = (json.dumps(incomplete, sort_keys=True) + "\n").encode()
            members["profile"].write_bytes(profile_data)
            provenance = members["provenance"].read_text()
            provenance = re.sub(
                r"profile_metadata=.*$",
                f"profile_metadata={IMAGE_NAME}.profile.json sha256={sha(profile_data)}",
                provenance,
                flags=re.MULTILINE,
            )
            members["provenance"].write_text(provenance)
            with self.assertRaisesRegex(bundle.BundleError, "metadata contract"):
                bundle.publish_bundle(
                    output_dir=root,
                    image_name=IMAGE_NAME,
                    profile="lab-no-acm",
                    temporary_members=members,
                )
            names = bundle.bundle_names(IMAGE_NAME, "lab-no-acm")
            self.assertFalse(any((root / name).exists() for name in names.values()))


if __name__ == "__main__":
    unittest.main()
