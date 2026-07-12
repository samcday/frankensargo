#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import os
import stat
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "bin" / "build-duranium-lvm-adapter"
PARTUUID = "db04e713-11c3-4d68-bec2-8cc483bd3891"
PV_UUID = "AAAAAA-bbbb-cccc-dddd-eeee-ffff-GGGGGG"
VG_UUID = "BBBBBB-1111-2222-3333-4444-5555-CCCCCC"
LV_UUID = "CCCCCC-aaaa-bbbb-cccc-dddd-eeee-FFFFFF"


def aarch64_elf(interpreter: str | None = None) -> bytes:
    phnum = 1 if interpreter else 0
    phoff = 64 if interpreter else 0
    data = bytearray(64 + 56 * phnum)
    data[:16] = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    struct.pack_into(
        "<HHIQQQIHHHHHH",
        data,
        16,
        3,
        183,
        1,
        0,
        phoff,
        0,
        0,
        64,
        56,
        phnum,
        0,
        0,
        0,
    )
    if interpreter:
        raw = interpreter.encode() + b"\0"
        offset = len(data)
        struct.pack_into("<IIQQQQQQ", data, 64, 3, 4, offset, 0, 0, len(raw), len(raw), 1)
        data.extend(raw)
    data.extend(b"fixture-payload")
    return bytes(data)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_newc(data: bytes):
    entries = []
    offset = 0
    while True:
        if data[offset : offset + 6] != b"070701":
            raise AssertionError(f"bad newc magic at {offset}")
        fields = [int(data[offset + 6 + i * 8 : offset + 14 + i * 8], 16) for i in range(13)]
        offset += 110
        namesize = fields[11]
        name_raw = data[offset : offset + namesize]
        if not name_raw.endswith(b"\0"):
            raise AssertionError("newc name is not NUL terminated")
        name = name_raw[:-1].decode()
        offset += namesize
        offset += -offset % 4
        size = fields[6]
        contents = data[offset : offset + size]
        offset += size
        offset += -offset % 4
        entries.append(
            {
                "name": name,
                "ino": fields[0],
                "mode": fields[1],
                "uid": fields[2],
                "gid": fields[3],
                "mtime": fields[5],
                "data": contents,
            }
        )
        if name == "TRAILER!!!":
            break
    if any(data[offset:]):
        raise AssertionError("nonzero bytes after newc trailer")
    return entries


class AdapterFixture:
    def __init__(self, root: Path, dynamic_thin: bool = True):
        self.root = root
        self.lvm = root / "lvm.static"
        self.thin = root / "thin-check-input"
        self.loader = root / "loader"
        self.libgcc = root / "libgcc"
        self.libudev = root / "libudev"
        self.lvm.write_bytes(aarch64_elf())
        self.thin.write_bytes(
            aarch64_elf("/lib/ld-musl-aarch64.so.1") if dynamic_thin else aarch64_elf()
        )
        self.loader.write_bytes(aarch64_elf())
        self.libgcc.write_bytes(aarch64_elf())
        self.libudev.write_bytes(aarch64_elf())

    def command(self, output: Path, include_closure: bool = True):
        command = [
            str(BUILDER),
            "--userdata-partuuid",
            PARTUUID,
            "--pv-uuid",
            PV_UUID,
            "--vg-uuid",
            VG_UUID,
            "--disk-lv-uuid",
            LV_UUID,
            "--disk-lv-name",
            "disk-duranium",
            "--disk-lv-tag",
            "pocketboot.disk.v1",
            "--lvm-static",
            str(self.lvm),
            "--lvm-static-sha256",
            digest(self.lvm),
            "--thin-check",
            str(self.thin),
            "--thin-check-sha256",
            digest(self.thin),
            "--output",
            str(output),
        ]
        if include_closure:
            for option, path in (
                ("thin-loader", self.loader),
                ("thin-libgcc", self.libgcc),
                ("thin-libudev", self.libudev),
            ):
                command.extend([f"--{option}", str(path), f"--{option}-sha256", digest(path)])
        return command


class DuraniumAdapterTests(unittest.TestCase):
    def run_builder(self, command, success=True):
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        if success and result.returncode != 0:
            self.fail(f"builder failed:\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
        if not success and result.returncode == 0:
            self.fail("builder unexpectedly succeeded")
        return result

    def test_dynamic_overlay_is_deterministic_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = AdapterFixture(root)
            first = root / "first.cpio"
            second = root / "second.cpio"
            self.run_builder(fixture.command(first))
            for path in (fixture.lvm, fixture.thin, fixture.loader, fixture.libgcc, fixture.libudev):
                os.utime(path, (1_900_000_000, 1_900_000_000))
            self.run_builder(fixture.command(second))
            self.assertEqual(first.read_bytes(), second.read_bytes())
            self.assertEqual(len(first.read_bytes()) % 512, 0)

            parsed = parse_newc(first.read_bytes())
            names = [entry["name"] for entry in parsed]
            self.assertEqual(names[:-1], sorted(names[:-1]))
            self.assertEqual(names[-1], "TRAILER!!!")
            self.assertTrue(all(entry["uid"] == entry["gid"] == 0 for entry in parsed))
            self.assertTrue(all(entry["mtime"] == 0 for entry in parsed))
            entries = {entry["name"]: entry for entry in parsed}

            expected_paths = {
                "init",
                "etc/frankensargo/duranium-lvm.conf",
                "etc/frankensargo/lvm/lvm.conf.template",
                "usr/lib/frankensargo-duranium/lvm.static",
                "usr/lib/frankensargo-duranium/thin_check",
                "usr/lib/frankensargo-duranium/ld-musl-aarch64.so.1",
                "usr/lib/frankensargo-duranium/libc.musl-aarch64.so.1",
                "usr/lib/frankensargo-duranium/libgcc_s.so.1",
                "usr/lib/frankensargo-duranium/libudev.so.1",
                "usr/lib/systemd/system-generators/00-frankensargo-duranium-lvm-generator",
                "usr/lib/systemd/system/frankensargo-duranium-lvm.service",
                "usr/libexec/frankensargo-duranium-lvm-attach",
                "usr/sbin/thin_check",
            }
            self.assertTrue(expected_paths.issubset(entries))
            self.assertTrue(stat.S_ISREG(entries["init"]["mode"]))
            self.assertEqual(stat.S_IMODE(entries["init"]["mode"]), 0o755)
            self.assertTrue(
                stat.S_ISLNK(
                    entries["usr/lib/frankensargo-duranium/libc.musl-aarch64.so.1"]["mode"]
                )
            )

            config = entries["etc/frankensargo/duranium-lvm.conf"]["data"].decode()
            for value in (PARTUUID, PV_UUID, VG_UUID, LV_UUID, "disk-duranium", "pocketboot.disk.v1"):
                self.assertIn(value, config)
            self.assertIn(f"DURANIUM_LVM_SHA256={digest(fixture.lvm)}", config)

            lvm_config = entries["etc/frankensargo/lvm/lvm.conf.template"]["data"].decode()
            self.assertIn('auto_activation_volume_list = [ ]', lvm_config)
            self.assertIn('read_only_volume_list = [ "@pocketboot.disk.v1" ]', lvm_config)
            self.assertIn(
                'global {\n    thin_check_executable = "/usr/sbin/thin_check"',
                lvm_config,
            )
            activation = lvm_config.split("activation {", 1)[1].split("}", 1)[0]
            self.assertNotIn("thin_check_executable", activation)
            self.assertEqual(lvm_config.count('"r|.*|"'), 2)
            self.assertIn("@ANCHOR@", lvm_config)

            attach = entries["usr/libexec/frankensargo-duranium-lvm-attach"]["data"].decode()
            self.assertIn("/usr/lib/frankensargo-duranium/lvm.static", attach)
            self.assertIn('"$LVM" "$command" --devices "$ANCHOR"', attach)
            self.assertIn(
                'lvm_report lvchange --activate y "$VG_NAME/$DURANIUM_DISK_LV_NAME"',
                attach,
            )
            self.assertNotIn("vgchange", attach)
            self.assertIn("root=dissect", attach)
            self.assertIn("mount.usr=dissect", attach)
            self.assertIn("sysrq_always_enabled=1", attach)
            self.assertIn("--loop-ref rootdisk", attach)
            self.assertIn("--read-only --partscan", attach)
            self.assertIn("PARTUUID match count", attach)
            self.assertNotIn("[ -e /dev/null ]", attach)
            self.assertIn("mounted_as /proc proc", attach)
            self.assertIn("mounted_as /sys sysfs", attach)
            self.assertIn("mounted_as /dev devtmpfs", attach)
            self.assertIn("mounted_as /run tmpfs", attach)
            self.assertIn("/dev/disk/by-partuuid /dev/disk/by-loop-ref /dev/mapper", attach)

            init = entries["init"]["data"].decode()
            self.assertLess(init.index("--attach"), init.index("exec /usr/lib/systemd/systemd"))
            self.assertIn("--unit=emergency.target", init)
            generator = entries[
                "usr/lib/systemd/system-generators/00-frankensargo-duranium-lvm-generator"
            ]["data"].decode()
            self.assertIn("initrd-root-device.target", generator)
            self.assertIn("initrd-usr-fs.target", generator)
            self.assertIn(".requires", generator)
            generator_path = root / "generator"
            generator_path.write_bytes(
                entries[
                    "usr/lib/systemd/system-generators/00-frankensargo-duranium-lvm-generator"
                ]["data"]
            )
            generator_path.chmod(0o755)
            normal = root / "generated"
            early = root / "generated-early"
            late = root / "generated-late"
            for path in (normal, early, late):
                path.mkdir()
            subprocess.run(
                [str(generator_path), str(normal), str(early), str(late)],
                check=True,
            )
            expected_target = "/usr/lib/systemd/system/frankensargo-duranium-lvm.service"
            for target in ("initrd-root-device.target", "initrd-usr-fs.target"):
                link = normal / f"{target}.requires/frankensargo-duranium-lvm.service"
                self.assertTrue(link.is_symlink())
                self.assertEqual(os.readlink(link), expected_target)
            service = entries[
                "usr/lib/systemd/system/frankensargo-duranium-lvm.service"
            ]["data"].decode()
            self.assertIn("--verify-active", service)
            self.assertIn("Before=initrd-root-device.target initrd-usr-fs.target", service)
            self.assertIn("systemd-repart.service", service)

            wrapper = entries["usr/sbin/thin_check"]["data"].decode()
            self.assertIn("--library-path", wrapper)
            self.assertIn('"$RUNTIME/thin_check"', wrapper)
            manifest = entries[
                "usr/lib/frankensargo-duranium/build-manifest.json"
            ]["data"].decode()
            self.assertIn('"format": "frankensargo.duranium-lvm-adapter.v1"', manifest)
            self.assertIn('"sysrq_always_enabled=1"', manifest)

    def test_static_thin_check_needs_no_runtime_closure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = AdapterFixture(root, dynamic_thin=False)
            output = root / "static.cpio"
            self.run_builder(fixture.command(output, include_closure=False))
            entries = {entry["name"]: entry for entry in parse_newc(output.read_bytes())}
            self.assertEqual(entries["usr/sbin/thin_check"]["data"], fixture.thin.read_bytes())
            self.assertNotIn("usr/lib/frankensargo-duranium/ld-musl-aarch64.so.1", entries)

    def test_rejects_missing_closure_bad_hash_and_nonstatic_lvm(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = AdapterFixture(root)
            missing = root / "missing.cpio"
            result = self.run_builder(fixture.command(missing, include_closure=False), success=False)
            self.assertIn("dynamic thin_check requires", result.stderr)
            self.assertFalse(missing.exists())

            bad_hash = root / "bad-hash.cpio"
            command = fixture.command(bad_hash)
            command[command.index("--lvm-static-sha256") + 1] = "0" * 64
            result = self.run_builder(command, success=False)
            self.assertIn("SHA-256 mismatch", result.stderr)
            self.assertFalse(bad_hash.exists())

            fixture.lvm.write_bytes(aarch64_elf("/lib/ld-musl-aarch64.so.1"))
            dynamic_lvm = root / "dynamic-lvm.cpio"
            result = self.run_builder(fixture.command(dynamic_lvm), success=False)
            self.assertIn("is not static", result.stderr)
            self.assertFalse(dynamic_lvm.exists())

    def test_rejects_ambiguous_identity_and_preserves_existing_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture = AdapterFixture(root)
            output = root / "adapter.cpio"
            command = fixture.command(output)
            self.run_builder(command)
            original = output.read_bytes()
            result = self.run_builder(command, success=False)
            self.assertIn("output exists", result.stderr)
            self.assertEqual(output.read_bytes(), original)

            uppercase = root / "uppercase.cpio"
            command = fixture.command(uppercase)
            command[command.index("--userdata-partuuid") + 1] = PARTUUID.upper()
            result = self.run_builder(command, success=False)
            self.assertIn("canonical lowercase", result.stderr)
            self.assertFalse(uppercase.exists())


if __name__ == "__main__":
    unittest.main()
