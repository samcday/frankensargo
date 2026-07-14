"""Fail-closed host client for PocketBoot's ADB shell-v2 service.

The ordinary ``adb shell`` process exits zero for every completed command when
the device falls back to the legacy shell service. That makes its process
status unsafe as a commit signal. This client requires the standard ADB
``shell_v2`` feature and wraps every command so a real remote success is
reported as an otherwise-unused, non-zero shell-v2 exit status. A legacy
transport therefore returns zero and is rejected instead of being mistaken
for success.
"""

from __future__ import annotations

import dataclasses
import secrets
import shlex
import shutil
import subprocess
from typing import BinaryIO, Sequence


SHELL_V2_FEATURE = b"shell_v2"
SUCCESS_SENTINEL = 173
FAILURE_SENTINEL = 174
TRUNCATED_SENTINEL = 255

# This shell remains alive after the child command and maps the child's status
# into a pair of values reserved by this protocol. In particular, a successful
# child never exits zero: legacy ADB's unconditional host status zero is thus
# impossible to accept as a successful attested command.
STATUS_WRAPPER = f"""\
"$@"
frankensargo_remote_status=$?
if [ "$frankensargo_remote_status" -eq 0 ]; then
    exit {SUCCESS_SENTINEL}
fi
exit {FAILURE_SENTINEL}
"""


class ShellV2Error(RuntimeError):
    """The host could not prove a complete shell-v2 command result."""


@dataclasses.dataclass(frozen=True)
class ShellV2Result:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


def _safe_serial(serial: str) -> str:
    if not serial or serial.startswith("-"):
        raise ShellV2Error("ADB serial is empty or option-like")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
    if any(character not in allowed for character in serial):
        raise ShellV2Error("ADB serial contains unsafe characters")
    return serial


def _validate_argv(argv: Sequence[str]) -> tuple[str, ...]:
    if (
        isinstance(argv, (str, bytes))
        or not argv
        or any(not isinstance(item, str) or not item for item in argv)
    ):
        raise ShellV2Error("refusing an empty or malformed remote argv")
    if any(any(character in item for character in "\x00\r\n") for item in argv):
        raise ShellV2Error("refusing a remote argv containing control separators")
    return tuple(argv)


def _shell_command(argv: Sequence[str]) -> str:
    """Serialize argv for adb's string-valued shell service without injection."""

    return " ".join(shlex.quote(item) for item in argv)


class AdbShellV2:
    """Reusable, status-attested PocketBoot command transport.

    ``stdin`` can be a regular file opened by the caller, so the same primitive
    can stream a large image into a remote ``dd`` without buffering it in host
    memory. Stdout and stderr are kept separate by the shell-v2 wire protocol.
    """

    def __init__(self, executable: str, serial: str) -> None:
        self.serial = _safe_serial(serial)
        resolved = shutil.which(executable)
        if resolved is None:
            raise ShellV2Error(f"adb executable was not found: {executable}")
        self.executable = resolved
        self._verified = False

    def _host(
        self,
        arguments: Sequence[str],
        *,
        timeout: int | None,
        stdin: BinaryIO | int | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        source: BinaryIO | int = subprocess.DEVNULL if stdin is None else stdin
        try:
            return subprocess.run(
                [self.executable, "-s", self.serial, *arguments],
                stdin=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ShellV2Error(
                f"adb command timed out: {' '.join(arguments[:2])}"
            ) from error
        except OSError as error:
            raise ShellV2Error(f"could not execute adb: {error}") from error

    def _invoke_attested(
        self,
        argv: Sequence[str],
        *,
        timeout: int | None,
        stdin: BinaryIO | int | None = None,
    ) -> ShellV2Result:
        command = _validate_argv(argv)
        wrapped = (
            "/bin/sh",
            "-c",
            STATUS_WRAPPER,
            "frankensargo-shell-v2",
            *command,
        )
        # -T makes the client request shell,v2,raw. Supplying one serialized
        # command is intentional: adb joins its remaining argv with spaces.
        result = self._host(
            ["shell", "-T", _shell_command(wrapped)],
            timeout=timeout,
            stdin=stdin,
        )

        if result.returncode == SUCCESS_SENTINEL:
            return ShellV2Result(command, 0, result.stdout, result.stderr)
        if result.returncode == FAILURE_SENTINEL:
            return ShellV2Result(command, 1, result.stdout, result.stderr)
        if result.returncode == 0:
            raise ShellV2Error(
                "ADB returned legacy status zero; shell_v2 remote status is unproven"
            )
        if result.returncode == TRUNCATED_SENTINEL:
            raise ShellV2Error(
                "ADB shell_v2 stream closed without a complete exit-status packet"
            )
        raise ShellV2Error(
            "ADB did not return the reserved shell_v2 status sentinel "
            f"(host status {result.returncode})"
        )

    def verify(self, *, timeout: int | None = 30) -> None:
        """Prove negotiated v2 status, stream separation, and freshness."""

        state = self._host(["get-state"], timeout=15)
        if state.returncode != 0 or state.stdout.decode("ascii", "replace").strip() not in (
            "device",
            "recovery",
        ):
            raise ShellV2Error("ADB endpoint is not a ready device or recovery transport")
        reported = self._host(["get-serialno"], timeout=15)
        if (
            reported.returncode != 0
            or reported.stdout.decode("ascii", "replace").strip() != self.serial
        ):
            raise ShellV2Error("ADB endpoint did not report the exact selected serial")
        features = self._host(["features"], timeout=15)
        if features.returncode != 0:
            raise ShellV2Error("could not query ADB device features")
        advertised = set(features.stdout.split())
        if SHELL_V2_FEATURE not in advertised:
            raise ShellV2Error("PocketBoot does not advertise the ADB shell_v2 feature")

        nonce = secrets.token_hex(32)
        stdout_token = f"frankensargo-shell-v2-stdout:{nonce}\n".encode()
        stderr_token = f"frankensargo-shell-v2-stderr:{nonce}\n".encode()
        probe = self._invoke_attested(
            [
                "/bin/sh",
                "-c",
                'printf "%s\\n" "$1"; printf "%s\\n" "$2" >&2; exit 0',
                "frankensargo-shell-v2-probe",
                stdout_token.rstrip(b"\n").decode("ascii"),
                stderr_token.rstrip(b"\n").decode("ascii"),
            ],
            timeout=timeout,
        )
        if probe.returncode != 0:
            raise ShellV2Error("shell_v2 status probe returned remote failure")
        if probe.stdout != stdout_token or probe.stderr != stderr_token:
            raise ShellV2Error(
                "shell_v2 probe output was stale, truncated, merged, or ambiguously framed"
            )
        self._verified = True

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: int | None = 120,
        stdin: BinaryIO | int | None = None,
    ) -> ShellV2Result:
        if not self._verified:
            probe_timeout = 30 if timeout is None else min(timeout, 30)
            self.verify(timeout=probe_timeout)
        return self._invoke_attested(argv, timeout=timeout, stdin=stdin)

    @property
    def verified(self) -> bool:
        return self._verified
