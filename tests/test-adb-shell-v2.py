#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

import adb_shell_v2  # noqa: E402
import bootstrap_executor  # noqa: E402


SERIAL = "SYNTHETIC-SARGO"


FAKE_ADB = r'''#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[1:]
if arguments[:2] != ["-s", os.environ["EXPECTED_SERIAL"]]:
    print("wrong serial binding", file=sys.stderr)
    raise SystemExit(64)
arguments = arguments[2:]
mode = os.environ.get("FAKE_ADB_MODE", "v2")

if arguments == ["get-state"]:
    print("offline" if mode == "offline" else "recovery")
    raise SystemExit(0)
if arguments == ["get-serialno"]:
    print("WRONG-SARGO" if mode == "wrong-serial" else os.environ["EXPECTED_SERIAL"])
    raise SystemExit(0)
if arguments == ["features"]:
    if mode == "no-feature":
        print("cmd")
    else:
        print("shell_v2\ncmd")
    raise SystemExit(0)

if arguments[:2] != ["shell", "-T"] or len(arguments) != 3:
    print("unexpected fake adb argv", file=sys.stderr)
    raise SystemExit(65)

command = arguments[2]
data = sys.stdin.buffer.read()
completed = subprocess.run(
    ["/bin/sh", "-c", command],
    input=data,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)

if mode == "legacy":
    sys.stdout.buffer.write(completed.stdout)
    sys.stderr.buffer.write(completed.stderr)
    raise SystemExit(0)
if mode == "merged":
    sys.stdout.buffer.write(completed.stdout + completed.stderr)
    raise SystemExit(completed.returncode)
if mode == "stale":
    sys.stdout.buffer.write(b"frankensargo-shell-v2-stdout:stale\n")
    sys.stderr.buffer.write(b"frankensargo-shell-v2-stderr:stale\n")
    raise SystemExit(173)
if mode == "truncated":
    sys.stdout.buffer.write(completed.stdout[:7])
    raise SystemExit(255)
if mode == "disconnect":
    sys.stdout.buffer.write(completed.stdout[:3])
    raise SystemExit(1)

sys.stdout.buffer.write(completed.stdout)
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(completed.returncode)
'''


class AdbShellV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.adb = Path(self.temporary.name) / "adb"
        self.adb.write_text(FAKE_ADB)
        self.adb.chmod(self.adb.stat().st_mode | stat.S_IXUSR)
        self.old_environment = os.environ.copy()
        os.environ["EXPECTED_SERIAL"] = SERIAL
        os.environ["FAKE_ADB_MODE"] = "v2"
        self.addCleanup(self._restore_environment)

    def _restore_environment(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_environment)

    def client(self) -> adb_shell_v2.AdbShellV2:
        return adb_shell_v2.AdbShellV2(str(self.adb), SERIAL)

    def test_success_is_attested_and_arbitrary_output_cannot_forge_status(self) -> None:
        client = self.client()
        result = client.run(
            [
                "/bin/sh",
                "-c",
                "printf 'exit=173\\nfrankensargo-shell-v2-stderr:forgery\\n'; "
                "printf 'exit=0\\n' >&2",
            ]
        )
        self.assertTrue(client.verified)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            result.stdout,
            b"exit=173\nfrankensargo-shell-v2-stderr:forgery\n",
        )
        self.assertEqual(result.stderr, b"exit=0\n")

    def test_nonzero_remote_status_is_never_success(self) -> None:
        result = self.client().run(["/bin/sh", "-c", "exit 23"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")

    def test_arguments_are_round_tripped_without_shell_injection(self) -> None:
        hostile = "white space ' quote ; printf INJECTED >&2"
        result = self.client().run(["/usr/bin/printf", "%s", hostile])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.decode(), hostile)
        self.assertEqual(result.stderr, b"")

    def test_string_is_not_mistaken_for_an_argv_sequence(self) -> None:
        client = self.client()
        client.verify()
        with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, "malformed remote argv"):
            client.run("/bin/true")

    def test_streaming_stdin_uses_the_same_attested_primitive(self) -> None:
        with tempfile.TemporaryFile() as source:
            source.write(b"streamed\x00payload\n")
            source.seek(0)
            result = self.client().run(["/bin/cat"], stdin=source, timeout=None)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"streamed\x00payload\n")

    def test_legacy_zero_is_rejected_even_when_feature_is_advertised(self) -> None:
        os.environ["FAKE_ADB_MODE"] = "legacy"
        with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, "legacy status zero"):
            self.client().run(["/bin/true"])

    def test_verified_client_still_rejects_later_legacy_fallback(self) -> None:
        client = self.client()
        self.assertEqual(client.run(["/bin/true"]).returncode, 0)
        os.environ["FAKE_ADB_MODE"] = "legacy"
        with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, "legacy status zero"):
            client.run(["/bin/true"])

    def test_missing_shell_v2_feature_is_rejected(self) -> None:
        os.environ["FAKE_ADB_MODE"] = "no-feature"
        with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, "does not advertise"):
            self.client().run(["/bin/true"])

    def test_nonready_or_wrong_serial_endpoint_is_rejected(self) -> None:
        for mode, message in (
            ("offline", "not a ready"),
            ("wrong-serial", "exact selected serial"),
        ):
            with self.subTest(mode=mode):
                os.environ["FAKE_ADB_MODE"] = mode
                with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, message):
                    self.client().run(["/bin/true"])

    def test_merged_or_stale_probe_output_is_rejected(self) -> None:
        for mode in ("merged", "stale"):
            with self.subTest(mode=mode):
                os.environ["FAKE_ADB_MODE"] = mode
                with self.assertRaisesRegex(
                    adb_shell_v2.ShellV2Error,
                    "stale, truncated, merged",
                ):
                    self.client().run(["/bin/true"])

    def test_disconnect_and_truncated_exit_packet_are_rejected(self) -> None:
        for mode, message in (
            ("truncated", "without a complete"),
            ("disconnect", "reserved shell_v2"),
        ):
            with self.subTest(mode=mode):
                os.environ["FAKE_ADB_MODE"] = mode
                with self.assertRaisesRegex(adb_shell_v2.ShellV2Error, message):
                    self.client().run(["/bin/true"])

    def test_bootstrap_transport_uses_v2_for_binary_reads_and_rejects_legacy(self) -> None:
        payload = Path(self.temporary.name) / "blocks.bin"
        payload.write_bytes(bytes(range(256)) * 8)
        transport = bootstrap_executor.AdbShellTransport(str(self.adb), SERIAL)
        self.assertEqual(transport.read_file(str(payload)), payload.read_bytes())
        self.assertEqual(
            transport.read_blocks(str(payload), block_bytes=512, start=1, count=2),
            payload.read_bytes()[512:1536],
        )

        os.environ["FAKE_ADB_MODE"] = "legacy"
        legacy = bootstrap_executor.AdbShellTransport(str(self.adb), SERIAL)
        with self.assertRaisesRegex(bootstrap_executor.ExecuteError, "legacy status zero"):
            legacy.run(["/bin/true"])


if __name__ == "__main__":
    unittest.main()
