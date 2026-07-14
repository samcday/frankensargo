#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

import pocketboot_profile as profile  # noqa: E402


BASE = "pocketboot.log=debug pocketboot.acm sysrq_always_enabled=1"
EFFECTIVE = "pocketboot.log=debug sysrq_always_enabled=1"


def config(cmdline: str = BASE) -> bytes:
    return (
        'name = "Google Pixel 3a (sargo)"\n\n'
        '[bootimg]\n'
        'header_version = 2\n'
        f"cmdline = {json.dumps(cmdline)}\n"
    ).encode()


def image(cmdline: str, size: int = 4096) -> bytes:
    data = bytearray((offset * 37 + 11) & 0xFF for offset in range(size))
    data[:8] = profile.ANDROID_MAGIC
    data[40:44] = profile.EXPECTED_HEADER_VERSION.to_bytes(4, "little")
    packed = profile.encoded_android_cmdline(cmdline)
    first = packed[: profile.ANDROID_CMDLINE_BYTES]
    second = packed[profile.ANDROID_CMDLINE_BYTES :]
    data[profile.CMDLINE_RANGES[0][0] : profile.CMDLINE_RANGES[0][1]] = first
    data[profile.CMDLINE_RANGES[1][0] : profile.CMDLINE_RANGES[1][1]] = second
    return bytes(data)


class PocketBootProfileTests(unittest.TestCase):
    def test_reads_real_nested_cmdline_and_removes_one_exact_acm_token(self) -> None:
        self.assertEqual(profile.config_cmdlines(config()), (BASE, EFFECTIVE))
        for bad, message in (
            (BASE.replace(" pocketboot.acm", ""), "exactly one"),
            (BASE + " pocketboot.acm", "exactly one"),
            (BASE + " pocketboot.acm=1", "ambiguous"),
            (BASE.replace(" sysrq_always_enabled=1", ""), "sysrq"),
        ):
            with self.subTest(bad=bad), self.assertRaisesRegex(
                profile.ProfileError, message
            ):
                profile.config_cmdlines(config(bad))

    def test_rejects_top_level_only_or_noncanonical_cmdline(self) -> None:
        with self.assertRaisesRegex(profile.ProfileError, "top-level"):
            profile.config_cmdlines(f"cmdline = {json.dumps(BASE)}\n".encode())
        noncanonical = config().replace(b"cmdline = ", b"cmdline=", 1)
        with self.assertRaisesRegex(profile.ProfileError, "canonical TOML line"):
            profile.config_cmdlines(noncanonical)

    def test_rewrite_changes_only_android_v2_cmdline_ranges(self) -> None:
        long_base = BASE + " " + "x" * 700
        original = image(long_base)
        rendered, metadata = profile.rewrite_android_boot_no_acm(original)
        self.assertEqual(len(rendered), len(original))
        self.assertEqual(
            profile.android_boot_cmdline(rendered),
            profile.without_acm_cmdline(long_base),
        )
        allowed = set()
        for start, end in profile.CMDLINE_RANGES:
            allowed.update(range(start, end))
        changed = {index for index, pair in enumerate(zip(original, rendered)) if pair[0] != pair[1]}
        self.assertTrue(changed)
        self.assertTrue(changed <= allowed)
        self.assertTrue(any(index >= profile.ANDROID_EXTRA_CMDLINE_OFFSET for index in changed))
        self.assertEqual(metadata["parent_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(metadata["result_sha256"], hashlib.sha256(rendered).hexdigest())
        self.assertIs(metadata["all_other_bytes_unchanged"], True)

    def test_rewrite_rejects_wrong_header_padding_and_missing_token(self) -> None:
        wrong = bytearray(image(BASE))
        wrong[40:44] = (1).to_bytes(4, "little")
        with self.assertRaisesRegex(profile.ProfileError, "header version"):
            profile.rewrite_android_boot_no_acm(bytes(wrong))
        padding = bytearray(image(BASE))
        padding[profile.CMDLINE_RANGES[1][1] - 1] = 1
        with self.assertRaisesRegex(profile.ProfileError, "noncanonical padding"):
            profile.rewrite_android_boot_no_acm(bytes(padding))
        with self.assertRaisesRegex(profile.ProfileError, "exactly one"):
            profile.rewrite_android_boot_no_acm(image(EFFECTIVE))
        sealed = bytearray(image(BASE))
        sealed[-64:-60] = profile.AVB_FOOTER_MAGIC
        with self.assertRaisesRegex(profile.ProfileError, "AVB-sealed"):
            profile.rewrite_android_boot_no_acm(bytes(sealed))

    def test_cli_atomically_rewrites_temp_image_and_emits_bound_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "pocketboot.img.tmp"
            metadata = root / "profile.json.tmp"
            original = image(BASE)
            artifact.write_bytes(original)
            command = [
                sys.executable,
                str(ROOT / "lib" / "pocketboot_profile.py"),
                "rewrite-image-no-acm",
                "--image",
                str(artifact),
                "--metadata",
                str(metadata),
            ]
            result = subprocess.run(command, check=True, text=True, capture_output=True)
            value = json.loads(metadata.read_text())
            self.assertEqual(result.stdout.strip(), hashlib.sha256(original).hexdigest())
            self.assertEqual(value["base_cmdline"], BASE)
            self.assertEqual(value["effective_cmdline"], EFFECTIVE)
            self.assertEqual(
                value["result_sha256"], hashlib.sha256(artifact.read_bytes()).hexdigest()
            )
            value["result_sha256"] = value["parent_sha256"]
            with self.assertRaisesRegex(profile.ProfileError, "hashes are equal"):
                profile.validate_metadata(value)


if __name__ == "__main__":
    unittest.main()
