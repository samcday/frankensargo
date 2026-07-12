"""Fail-closed executor for a bootstrap-plan v1 userdata-anchor transaction.

The planner deliberately emits inert argv.  This module is the separately
armed host controller which can execute those argv over an already connected
PocketBoot ADB shell.  Its transport and evidence providers are injectable so
the transaction engine can be exercised without a phone.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
from typing import Callable, Protocol, Sequence
import uuid
import zlib

import jsonschema

import adb_shell_v2
import bootstrap_plan
import pbread1


ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schema/bootstrap-plan-v1.schema.json"
RECOVERY_ATTESTATION = "ABL-AND-SYSRQ-RECOVERY-PROVEN"
REMOTE_ROOT = "/run/frankensargo-bootstrap"
MAX_PLAN_BYTES = 8 * 1024 * 1024
MAX_REMOTE_FILE_BYTES = 8 * 1024 * 1024
GPT_HEADER = struct.Struct("<8sIIIIQQQQ16sQIII")
KERNEL_NAME_RE = re.compile(r"^mmcblk[0-9]+p[1-9][0-9]*$")
TARGET_DISK_NAME = "mmcblk0"
TARGET_PARTITION_NAME = "mmcblk0p72"
TARGET_DEVICE_PATH = "/dev/mmcblk0p72"
TARGET_PARTITION_NUMBER = 72
SAFE_REMOTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]+$")
UUID_FIELD_RE = bootstrap_plan.LVM_UUID_RE
VOLATILE_HOST_FILESYSTEMS = {
    "tmpfs",
    "ramfs",
    "overlay",
    "squashfs",
}


class ExecuteError(RuntimeError):
    """A safety gate, transport operation, or exact state check failed."""


@dataclasses.dataclass(frozen=True)
class RemoteResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


class ShellTransport(Protocol):
    """Minimal remote interface used by the production and fake executors."""

    serial: str

    def connection_state(self) -> str: ...

    def reported_serial(self) -> str: ...

    def run(self, argv: Sequence[str], *, timeout: int = 120) -> RemoteResult: ...

    def read_file(self, path: str, *, maximum: int = MAX_REMOTE_FILE_BYTES) -> bytes: ...

    def list_dir(self, path: str) -> list[str]: ...

    def read_blocks(
        self,
        path: str,
        *,
        block_bytes: int,
        start: int,
        count: int,
    ) -> bytes: ...


def _safe_serial(serial: str) -> str:
    if not pbread1.SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
        raise ExecuteError("serial contains unsafe characters")
    return serial


def _safe_remote_path(path: str) -> str:
    if not SAFE_REMOTE_PATH_RE.fullmatch(path) or "//" in path or "/../" in path:
        raise ExecuteError(f"unsafe remote path: {path!r}")
    return path


class AdbShellTransport:
    """PocketBoot shell transport; every host adb call carries one serial."""

    def __init__(self, executable: str, serial: str) -> None:
        self.serial = _safe_serial(serial)
        resolved = shutil.which(executable)
        if resolved is None:
            raise ExecuteError(f"adb executable was not found: {executable}")
        self.executable = resolved
        self.shell_v2 = adb_shell_v2.AdbShellV2(resolved, self.serial)

    def _host(
        self,
        arguments: Sequence[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.executable, "-s", self.serial, *arguments],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise ExecuteError(f"adb command timed out: {' '.join(arguments)}") from error
        except OSError as error:
            raise ExecuteError(f"could not execute adb: {error}") from error

    def connection_state(self) -> str:
        result = self._host(["get-state"], timeout=15)
        if result.returncode != 0:
            raise ExecuteError(f"adb get-state failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout.decode("ascii", errors="strict").strip()

    def reported_serial(self) -> str:
        result = self._host(["get-serialno"], timeout=15)
        if result.returncode != 0:
            raise ExecuteError(f"adb get-serialno failed: {result.stderr.decode(errors='replace').strip()}")
        return result.stdout.decode("ascii", errors="strict").strip()

    def run(self, argv: Sequence[str], *, timeout: int = 120) -> RemoteResult:
        try:
            result = self.shell_v2.run(argv, timeout=timeout)
        except adb_shell_v2.ShellV2Error as error:
            raise ExecuteError(f"untrusted ADB remote command result: {error}") from error
        return RemoteResult(result.argv, result.returncode, result.stdout, result.stderr)

    def read_file(self, path: str, *, maximum: int = MAX_REMOTE_FILE_BYTES) -> bytes:
        path = _safe_remote_path(path)
        result = self.run(["/bin/cat", path], timeout=120)
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace").strip()
            raise ExecuteError(f"could not pull {path}: {detail}")
        if len(result.stdout) > maximum:
            raise ExecuteError(f"remote file exceeds {maximum} bytes: {path}")
        return result.stdout

    def list_dir(self, path: str) -> list[str]:
        path = _safe_remote_path(path)
        result = self.run(["/bin/ls", "-1A", path], timeout=30)
        if result.returncode != 0:
            raise ExecuteError(f"could not list remote directory {path}")
        try:
            text = result.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExecuteError(f"remote directory listing is not UTF-8: {path}") from error
        names = [line for line in text.splitlines() if line]
        for name in names:
            if "/" in name or name in (".", "..") or "\x00" in name:
                raise ExecuteError(f"unsafe name in remote directory {path}: {name!r}")
        return names

    def read_blocks(
        self,
        path: str,
        *,
        block_bytes: int,
        start: int,
        count: int,
    ) -> bytes:
        path = _safe_remote_path(path)
        if block_bytes not in (512, 1024, 2048, 4096):
            raise ExecuteError("unsupported remote block size")
        if start < 0 or count < 1 or count * block_bytes > 16 * 1024 * 1024:
            raise ExecuteError("remote block read is outside the bounded safety envelope")
        result = self.run(
            [
                "/bin/dd",
                f"if={path}",
                f"bs={block_bytes}",
                f"skip={start}",
                f"count={count}",
            ],
            timeout=60,
        )
        expected = block_bytes * count
        if result.returncode != 0 or len(result.stdout) != expected:
            raise ExecuteError(
                f"short or failed bounded read of {path}: expected {expected}, "
                f"received {len(result.stdout)}"
            )
        return result.stdout


def canonical_bytes(value: object) -> bytes:
    return bootstrap_plan.canonical_json_bytes(value)


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _open_regular_nofollow(path: Path, maximum: int, field: str) -> bytes:
    flags = os.O_RDONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ExecuteError(f"cannot open {field} {path}: {error}") from error
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ExecuteError(f"{field} is not a regular file: {path}")
        if before.st_size > maximum:
            raise ExecuteError(f"{field} exceeds {maximum} bytes")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ExecuteError(f"{field} ended while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise ExecuteError(f"{field} grew while being read")
        after = os.fstat(descriptor)
        identity = lambda value: (  # noqa: E731 - compact immutable tuple
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ExecuteError(f"{field} changed while being read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def load_and_validate_plan(path: Path) -> tuple[dict[str, object], str]:
    raw = _open_regular_nofollow(path, MAX_PLAN_BYTES, "bootstrap plan")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecuteError(f"bootstrap plan is not valid JSON: {error}") from error
    if not isinstance(value, dict):
        raise ExecuteError("bootstrap plan is not a JSON object")
    try:
        schema = json.loads(SCHEMA_PATH.read_text())
        jsonschema.Draft202012Validator(schema).validate(value)
    except (OSError, json.JSONDecodeError, jsonschema.ValidationError) as error:
        raise ExecuteError(f"bootstrap plan does not satisfy schema v1: {error}") from error

    core = dict(value)
    recorded = core.pop("authorization_sha256")
    confirmation = core.pop("confirmation")
    actual = sha256_bytes(canonical_bytes(core))
    if recorded != actual:
        raise ExecuteError(
            f"plan authorization hash mismatch: recorded {recorded}, calculated {actual}"
        )
    operation = str(value["operation_uuid"])
    expected_token = f"BOOTSTRAP-{operation.split('-', 1)[0]}-{actual[7:19]}"
    if not isinstance(confirmation, dict) or confirmation.get("token") != expected_token:
        raise ExecuteError("plan confirmation token is not derived from its authorization hash")
    return value, sha256_bytes(raw)


def _decimal(value: object, field: str) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]*", value):
        return int(value)
    raise ExecuteError(f"{field} is not a canonical nonnegative integer: {value!r}")


def _text(data: bytes, field: str, *, strip_nul: bool = False) -> str:
    if strip_nul:
        data = data.rstrip(b"\x00\r\n")
    else:
        data = data.rstrip(b"\r\n")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExecuteError(f"{field} is not UTF-8") from error


def _uevent(data: bytes, field: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in _text(data, field).splitlines():
        if "=" not in line:
            raise ExecuteError(f"malformed {field} line: {line!r}")
        key, value = line.split("=", 1)
        if not re.fullmatch(r"[A-Z0-9_]+", key) or key in result:
            raise ExecuteError(f"malformed or duplicate {field} key: {key!r}")
        result[key] = value
    return result


@dataclasses.dataclass(frozen=True)
class GptHeader:
    current_lba: int
    backup_lba: int
    first_usable_lba: int
    last_usable_lba: int
    disk_guid: str
    entry_lba: int
    entry_count: int
    entry_size: int
    entry_crc32: int


def _parse_gpt_header(raw: bytes, expected_lba: int, disk_sectors: int, role: str) -> GptHeader:
    if len(raw) != 512:
        raise ExecuteError(f"{role} GPT header is not one 512-byte sector")
    values = GPT_HEADER.unpack_from(raw)
    (
        signature,
        revision,
        header_size,
        header_crc,
        reserved,
        current_lba,
        backup_lba,
        first_usable,
        last_usable,
        disk_guid_raw,
        entry_lba,
        entry_count,
        entry_size,
        entry_crc,
    ) = values
    if signature != b"EFI PART" or revision != 0x00010000:
        raise ExecuteError(f"{role} GPT signature or revision is invalid")
    if not GPT_HEADER.size <= header_size <= 512 or reserved != 0:
        raise ExecuteError(f"{role} GPT header size or reserved field is invalid")
    checked = bytearray(raw[:header_size])
    checked[16:20] = bytes(4)
    if zlib.crc32(checked) & 0xFFFFFFFF != header_crc:
        raise ExecuteError(f"{role} GPT header CRC does not match")
    if current_lba != expected_lba or not 0 <= backup_lba < disk_sectors:
        raise ExecuteError(f"{role} GPT current/backup LBA is invalid")
    if not 0 < first_usable <= last_usable < disk_sectors:
        raise ExecuteError(f"{role} GPT usable range is invalid")
    if not 0 < entry_lba < disk_sectors:
        raise ExecuteError(f"{role} GPT entry LBA is invalid")
    if not 1 <= entry_count <= 4096 or not 128 <= entry_size <= 4096 or entry_size % 8:
        raise ExecuteError(f"{role} GPT entry geometry is invalid")
    if entry_count * entry_size > 16 * 1024 * 1024:
        raise ExecuteError(f"{role} GPT entry array is too large")
    return GptHeader(
        current_lba,
        backup_lba,
        first_usable,
        last_usable,
        str(uuid.UUID(bytes_le=disk_guid_raw)),
        entry_lba,
        entry_count,
        entry_size,
        entry_crc,
    )


def _decode_gpt_name(raw: bytes) -> str:
    try:
        decoded = raw.decode("utf-16-le")
    except UnicodeDecodeError as error:
        raise ExecuteError("userdata GPT name is not UTF-16LE") from error
    name = decoded.split("\x00", 1)[0]
    if not name or any(ord(character) < 0x20 for character in name):
        raise ExecuteError("userdata GPT name prefix is empty or contains controls")
    return name


def _plan_dict(plan: dict[str, object], name: str) -> dict[str, object]:
    value = plan.get(name)
    if not isinstance(value, dict):
        raise ExecuteError(f"validated plan field {name} is not an object")
    return value


def check_block_device_binding(
    transport: ShellTransport,
    identity: dict[str, object],
) -> dict[str, object]:
    """Re-prove that the fixed pathname still names the exact sysfs dev_t."""

    if (
        identity.get("kernel_name") != TARGET_PARTITION_NAME
        or identity.get("parent_kernel_name") != TARGET_DISK_NAME
        or identity.get("device_path") != TARGET_DEVICE_PATH
    ):
        raise ExecuteError("live identity is not bound to fixed userdata path/parent")
    expected = str(identity.get("major_minor", ""))
    if not re.fullmatch(r"[0-9]+:[0-9]+", expected):
        raise ExecuteError("live userdata identity has a malformed device number")
    current = _text(
        transport.read_file(
            f"/sys/class/block/{TARGET_PARTITION_NAME}/dev", maximum=64
        ),
        "partition device number",
    )
    if current != expected:
        raise ExecuteError("userdata sysfs device number changed after identity binding")
    node = transport.run(
        ["/bin/stat", "-L", "-c", "%f:%t:%T", TARGET_DEVICE_PATH], timeout=15
    )
    if node.returncode != 0:
        raise ExecuteError("could not stat the fixed userdata device node")
    match = re.fullmatch(
        r"([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)",
        _text(node.stdout, "userdata device stat"),
    )
    if match is None:
        raise ExecuteError("userdata device stat output is malformed")
    mode, node_major, node_minor = (int(value, 16) for value in match.groups())
    sysfs_major, sysfs_minor = (int(value, 10) for value in current.split(":"))
    if stat.S_IFMT(mode) != stat.S_IFBLK:
        raise ExecuteError(f"{TARGET_DEVICE_PATH} is not a block device")
    if (node_major, node_minor) != (sysfs_major, sysfs_minor):
        raise ExecuteError("userdata device-node rdev differs from its bound sysfs partition")
    return {
        "device_path": TARGET_DEVICE_PATH,
        "major_minor": current,
        "block_mode": f"{mode:x}",
    }


def check_live_identity_and_geometry(
    transport: ShellTransport,
    plan: dict[str, object],
) -> dict[str, object]:
    device = _plan_dict(plan, "device")
    partition = _plan_dict(plan, "partition")
    if partition.get("kernel_name_observation") != TARGET_PARTITION_NAME:
        raise ExecuteError(
            f"plan target must be the fixed userdata node {TARGET_PARTITION_NAME}"
        )
    serial = str(device["fastboot_serial"])
    state = transport.connection_state()
    if state not in ("device", "recovery"):
        raise ExecuteError(f"ADB endpoint is not an available PocketBoot shell: {state!r}")
    if transport.reported_serial() != serial or transport.serial != serial:
        raise ExecuteError("explicit, transport, and device-reported serials disagree")
    root = transport.run(["/usr/bin/id"], timeout=15)
    if root.returncode != 0 or not root.stdout.startswith(b"uid=0"):
        raise ExecuteError("PocketBoot shell is not uid 0")
    compatibles = transport.read_file("/proc/device-tree/compatible", maximum=4096).rstrip(b"\x00").split(b"\x00")
    if b"google,sargo" not in compatibles:
        raise ExecuteError("live device-tree compatible does not identify google,sargo")
    cid = _text(transport.read_file("/sys/block/mmcblk0/device/cid", maximum=256), "eMMC CID")
    if cid != device["emmc_cid"]:
        raise ExecuteError("live eMMC CID does not match the plan")
    sector_bytes = _decimal(
        _text(transport.read_file("/sys/class/block/mmcblk0/queue/logical_block_size", maximum=64), "logical block size"),
        "logical block size",
    )
    if sector_bytes != partition["logical_sector_bytes"] or sector_bytes != 512:
        raise ExecuteError("live logical sector size does not match the plan")
    disk_512_sectors = _decimal(
        _text(transport.read_file("/sys/class/block/mmcblk0/size", maximum=64), "disk size"),
        "disk size",
    )
    disk_sectors = disk_512_sectors * 512 // sector_bytes

    parent_fields = _uevent(
        transport.read_file(f"/sys/class/block/{TARGET_DISK_NAME}/uevent", maximum=4096),
        f"{TARGET_DISK_NAME} uevent",
    )
    if (
        parent_fields.get("DEVNAME") != TARGET_DISK_NAME
        or parent_fields.get("DEVTYPE") != "disk"
    ):
        raise ExecuteError("live userdata parent is not the fixed mmcblk0 disk")

    matches: list[tuple[str, dict[str, str]]] = []
    for name in transport.list_dir("/sys/class/block"):
        if not KERNEL_NAME_RE.fullmatch(name):
            continue
        fields = _uevent(
            transport.read_file(f"/sys/class/block/{name}/uevent", maximum=4096),
            f"{name} uevent",
        )
        if fields.get("PARTUUID", "").lower() == partition["partuuid"]:
            matches.append((name, fields))
    if len(matches) != 1:
        raise ExecuteError("PARTUUID did not resolve to exactly one live sysfs partition")
    kernel_name, fields = matches[0]
    if kernel_name != TARGET_PARTITION_NAME:
        raise ExecuteError("live PARTUUID resolved to a different kernel node than the plan")
    if (
        fields.get("DEVNAME") != TARGET_PARTITION_NAME
        or fields.get("DEVTYPE") != "partition"
        or fields.get("PARTN") != str(TARGET_PARTITION_NUMBER)
        or fields.get("PARTNAME") != "userdata"
    ):
        raise ExecuteError("live sysfs partition name does not match userdata")
    partition_number = _decimal(
        _text(
            transport.read_file(
                f"/sys/class/block/{kernel_name}/partition", maximum=64
            ),
            "partition number",
        ),
        "partition number",
    )
    if partition_number != TARGET_PARTITION_NUMBER:
        raise ExecuteError("live sysfs partition number is not the fixed userdata partition")

    sysfs_path = transport.run(
        ["/bin/readlink", "-f", f"/sys/class/block/{kernel_name}"], timeout=15
    )
    if sysfs_path.returncode != 0:
        raise ExecuteError("could not resolve the live userdata sysfs parent")
    resolved_sysfs = _text(sysfs_path.stdout, "userdata sysfs path")
    sysfs_parts = tuple(part for part in resolved_sysfs.split("/") if part)
    if (
        not resolved_sysfs.startswith("/sys/devices/")
        or sysfs_parts[-2:] != (TARGET_DISK_NAME, TARGET_PARTITION_NAME)
    ):
        raise ExecuteError("live userdata sysfs node is not parented by mmcblk0")
    start = _decimal(
        _text(transport.read_file(f"/sys/class/block/{kernel_name}/start", maximum=64), "partition start"),
        "partition start",
    )
    sectors_512 = _decimal(
        _text(transport.read_file(f"/sys/class/block/{kernel_name}/size", maximum=64), "partition size"),
        "partition size",
    )
    if start != _decimal(partition["start_lba"], "plan start LBA"):
        raise ExecuteError("live userdata start LBA does not match the plan")
    if sectors_512 != _decimal(partition["sectors"], "plan sectors"):
        raise ExecuteError("live userdata sector count does not match the plan")
    if sectors_512 * 512 != _decimal(partition["raw_bytes"], "plan raw bytes"):
        raise ExecuteError("live userdata byte geometry does not match the plan")

    primary_raw = transport.read_blocks("/dev/mmcblk0", block_bytes=512, start=1, count=1)
    backup_raw = transport.read_blocks(
        "/dev/mmcblk0", block_bytes=512, start=disk_sectors - 1, count=1
    )
    if sha256_bytes(primary_raw) != device["gpt_primary_header_sha256"]:
        raise ExecuteError("live primary GPT header hash does not match the plan")
    if sha256_bytes(backup_raw) != device["gpt_backup_header_sha256"]:
        raise ExecuteError("live backup GPT header hash does not match the plan")
    primary = _parse_gpt_header(primary_raw, 1, disk_sectors, "primary")
    backup = _parse_gpt_header(backup_raw, disk_sectors - 1, disk_sectors, "backup")
    if primary.backup_lba != backup.current_lba or backup.backup_lba != 1:
        raise ExecuteError("live GPT primary/backup linkage is inconsistent")
    if primary.disk_guid != device["gpt_disk_guid"] or backup.disk_guid != primary.disk_guid:
        raise ExecuteError("live GPT disk GUID does not match the plan")
    geometry = (primary.entry_count, primary.entry_size, primary.entry_crc32)
    if (backup.entry_count, backup.entry_size, backup.entry_crc32) != geometry:
        raise ExecuteError("live primary and backup GPT entry geometry differs")
    if backup.entry_lba != primary.entry_lba:
        raise ExecuteError("live GPT no longer has the inventoried aliases-primary layout")
    entry_bytes = primary.entry_count * primary.entry_size
    entry_sectors = (entry_bytes + 511) // 512
    entries_padded = transport.read_blocks(
        "/dev/mmcblk0",
        block_bytes=512,
        start=primary.entry_lba,
        count=entry_sectors,
    )
    entries = entries_padded[:entry_bytes]
    if zlib.crc32(entries) & 0xFFFFFFFF != primary.entry_crc32:
        raise ExecuteError("live GPT entry-array CRC does not match")
    if sha256_bytes(entries) != device["gpt_entry_array_sha256"]:
        raise ExecuteError("live GPT entry-array hash does not match the plan")
    part_matches = []
    for index in range(primary.entry_count):
        entry = entries[index * primary.entry_size : (index + 1) * primary.entry_size]
        if entry[:16] == bytes(16):
            continue
        candidate = str(uuid.UUID(bytes_le=entry[16:32]))
        if candidate == partition["partuuid"]:
            part_matches.append(entry)
    if len(part_matches) != 1:
        raise ExecuteError("planned PARTUUID did not resolve exactly once in the live GPT")
    entry = part_matches[0]
    first, last = struct.unpack_from("<QQ", entry, 32)
    if str(uuid.UUID(bytes_le=entry[:16])) != partition["type_guid"]:
        raise ExecuteError("live userdata GPT type does not match the plan")
    if first != start or last - first + 1 != sectors_512:
        raise ExecuteError("live userdata GPT entry geometry does not match sysfs/plan")
    if _decode_gpt_name(entry[56:128]) != partition["partlabel"]:
        raise ExecuteError("live userdata GPT label prefix does not match the plan")

    dev = _text(
        transport.read_file(f"/sys/class/block/{kernel_name}/dev", maximum=64),
        "partition device number",
    )
    if not re.fullmatch(r"[0-9]+:[0-9]+", dev):
        raise ExecuteError("live userdata sysfs device number is malformed")
    identity = {
        "adb_state": state,
        "serial": serial,
        "compatible": "google,sargo",
        "emmc_cid": cid,
        "kernel_name": kernel_name,
        "device_path": TARGET_DEVICE_PATH,
        "major_minor": dev,
        "device_rdev": dev,
        "parent_kernel_name": TARGET_DISK_NAME,
        "partuuid": partition["partuuid"],
        "partlabel": partition["partlabel"],
        "start_lba": str(start),
        "sectors": str(sectors_512),
        "logical_sector_bytes": sector_bytes,
        "raw_bytes": str(sectors_512 * 512),
        "gpt_entry_array_sha256": sha256_bytes(entries),
        "gpt_primary_header_sha256": sha256_bytes(primary_raw),
        "gpt_backup_header_sha256": sha256_bytes(backup_raw),
    }
    identity["block_binding"] = check_block_device_binding(transport, identity)
    return identity


def check_quiescence(
    transport: ShellTransport,
    identity: dict[str, object],
) -> dict[str, object]:
    kernel_name = str(identity["kernel_name"])
    major_minor = str(identity["major_minor"])
    mountinfo = _text(transport.read_file("/proc/self/mountinfo", maximum=4 * 1024 * 1024), "mountinfo")
    mounted = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 6 or "-" not in fields:
            raise ExecuteError("live mountinfo contains a malformed record")
        if fields[2] == major_minor:
            mounted.append(line)
    swaps = _text(transport.read_file("/proc/swaps", maximum=1024 * 1024), "swaps")
    swap_lines = swaps.splitlines()
    if swap_lines and not swap_lines[0].startswith("Filename"):
        raise ExecuteError("live /proc/swaps has an unexpected header")
    # PocketBoot has no legitimate swap during takeover.  Rejecting every
    # active swap also avoids missing userdata through an unexpected alias.
    used_swap = [line for line in swap_lines[1:] if line.split()]
    holders = transport.list_dir(f"/sys/class/block/{kernel_name}/holders")
    if mounted:
        raise ExecuteError("userdata is mounted according to live mountinfo")
    if used_swap:
        raise ExecuteError("userdata is active swap")
    if holders:
        raise ExecuteError(f"userdata has live block holders: {', '.join(holders)}")
    return {"mounted": False, "swap": False, "holders": []}


def verify_runtime_artifacts(
    transport: ShellTransport,
    plan: dict[str, object],
) -> dict[str, object]:
    transaction = _plan_dict(plan, "transaction")
    artifacts = transaction.get("runtime_artifacts")
    if not isinstance(artifacts, dict):
        raise ExecuteError("validated plan runtime_artifacts is malformed")
    observed: dict[str, object] = {}
    for name in ("lvm_static", "lvm_conf"):
        expected = artifacts.get(name)
        if not isinstance(expected, dict):
            raise ExecuteError(f"validated runtime artifact {name} is malformed")
        size = _decimal(expected.get("bytes"), f"{name} expected bytes")
        data = transport.read_file(str(expected.get("path")), maximum=size + 1)
        if len(data) != size or sha256_bytes(data) != expected.get("sha256"):
            raise ExecuteError(f"live {name} bytes/hash do not match the plan")
        observed[name] = {
            "path": expected["path"],
            "bytes": str(len(data)),
            "sha256": sha256_bytes(data),
        }
    version = transport.run([str(transaction["lvm_binary"]), "version"], timeout=30)
    if version.returncode != 0:
        raise ExecuteError("live lvm.static version command failed")
    expected_version = str(artifacts["lvm_static"]["version"])
    version_text = (version.stdout + b"\n" + version.stderr).decode("utf-8", errors="replace")
    pattern = re.compile(rf"(?m)^\s*LVM version:\s*{re.escape(expected_version)}(?:\(|\s|$)")
    if not pattern.search(version_text):
        raise ExecuteError(f"live lvm.static did not report LVM {expected_version}")
    observed["lvm_static"]["version"] = expected_version
    observed["version_output_sha256"] = sha256_bytes(version.stdout + b"\x00" + version.stderr)
    return observed


class SourceVerifier(Protocol):
    def verify(self, plan: dict[str, object], state_dir: Path) -> str: ...


def _verify_pbread_run_locked(
    run_dir: Path,
) -> tuple[str, dict[str, object], str, dict[str, object], bytes]:
    manifest, manifest_digest = pbread1.load_manifest(run_dir)
    journal = pbread1.load_journal(run_dir, manifest_digest)
    binding = pbread1.PartitionBinding.from_manifest(manifest)
    pbread1.verify_chunks(run_dir, manifest, journal)
    assembled = journal.get("assembled")
    if not isinstance(assembled, dict):
        raise pbread1.BackupError("journal.assembled is not an object")
    recorded = pbread1.normalized_sha256(
        pbread1.require_str(assembled.get("sha256"), "assembled.sha256"),
        "assembled.sha256",
    )
    raw_path = run_dir / f"{binding.partlabel}.raw"
    try:
        raw_status = raw_path.lstat()
    except OSError as error:
        raise pbread1.BackupError(f"cannot inspect assembled raw image: {error}") from error
    if (
        stat.S_ISLNK(raw_status.st_mode)
        or not stat.S_ISREG(raw_status.st_mode)
        or raw_status.st_size != binding.partition_bytes
    ):
        raise pbread1.BackupError(
            "assembled raw image is missing, symlinked, or has the wrong size"
        )
    actual = pbread1.sha256_file(raw_path)
    if actual != recorded:
        raise pbread1.BackupError("assembled raw image does not match the journal")
    source = journal.get("source_verification")
    if not isinstance(source, dict) or source.get("status") != "matched":
        raise pbread1.BackupError("source verification is not matched")
    source_digest = pbread1.normalized_sha256(
        pbread1.require_str(source.get("source_sha256"), "source_sha256"),
        "source_sha256",
    )
    if source_digest != actual:
        raise pbread1.BackupError("source verification digest does not match the raw image")
    journal_raw = _open_regular_nofollow(
        run_dir / "journal.json", MAX_PLAN_BYTES, "PBREAD1 journal"
    )
    return actual, manifest, manifest_digest, journal, journal_raw


def verify_host_backup(
    plan: dict[str, object],
    run_dir: Path,
    *,
    lock_held: bool = False,
) -> dict[str, object]:
    if not run_dir.is_absolute() or run_dir.resolve() != run_dir:
        raise ExecuteError("PBREAD1 run directory must be an absolute canonical path")
    try:
        lock = contextlib.nullcontext() if lock_held else pbread1.run_lock(
            run_dir, exclusive=False
        )
        with lock, contextlib.redirect_stdout(io.StringIO()):
            raw_digest, manifest, manifest_digest, journal, journal_raw = (
                _verify_pbread_run_locked(run_dir)
            )
    except (OSError, pbread1.BackupError) as error:
        raise ExecuteError(f"PBREAD1 backup verification failed: {error}") from error
    artifacts = _plan_dict(plan, "artifacts")
    expected = artifacts.get("pbread1")
    if not isinstance(expected, dict):
        raise ExecuteError("validated plan PBREAD1 artifacts are malformed")
    actual_manifest = f"sha256:{manifest_digest}"
    actual_journal = sha256_bytes(journal_raw)
    actual_raw = f"sha256:{raw_digest}"
    if actual_manifest != expected.get("manifest_sha256"):
        raise ExecuteError("PBREAD1 manifest hash no longer matches the plan")
    if actual_journal != expected.get("journal_sha256"):
        raise ExecuteError("PBREAD1 journal hash no longer matches the plan")
    if actual_raw != expected.get("raw_sha256"):
        raise ExecuteError("PBREAD1 raw backup hash no longer matches the plan")
    if manifest.get("run_uuid") != expected.get("run_uuid"):
        raise ExecuteError("PBREAD1 run UUID no longer matches the plan")
    source = journal.get("source_verification")
    if not isinstance(source, dict):
        raise ExecuteError("PBREAD1 source verification record is malformed")
    if source.get("status") != "matched" or source.get("verified_at_utc") != expected.get("source_verified_at_utc"):
        raise ExecuteError("PBREAD1 source terminal state/timestamp no longer matches the plan")
    if source.get("source_sha256") != expected.get("raw_sha256"):
        raise ExecuteError("PBREAD1 source digest no longer matches its raw backup")
    partition = manifest.get("partition")
    device = manifest.get("device")
    planned_partition = _plan_dict(plan, "partition")
    planned_device = _plan_dict(plan, "device")
    if not isinstance(partition, dict) or not isinstance(device, dict):
        raise ExecuteError("PBREAD1 manifest identity is malformed")
    binding = {
        "partuuid": planned_partition["partuuid"],
        "type_guid": planned_partition["type_guid"],
        "partlabel": planned_partition["partlabel"],
        "kernel_name_observation": planned_partition["kernel_name_observation"],
        "start_lba": planned_partition["start_lba"],
        "sectors": planned_partition["sectors"],
        "logical_sector_bytes": planned_partition["logical_sector_bytes"],
        "raw_bytes": planned_partition["raw_bytes"],
    }
    for field, wanted in binding.items():
        if partition.get(field) != wanted:
            raise ExecuteError(f"PBREAD1 manifest {field} no longer matches the plan")
    if device.get("fastboot_serial") != planned_device["fastboot_serial"] or device.get("emmc_cid") != planned_device["emmc_cid"]:
        raise ExecuteError("PBREAD1 manifest device identity no longer matches the plan")
    return {
        "run_dir": str(run_dir),
        "run_uuid": manifest["run_uuid"],
        "manifest_sha256": actual_manifest,
        "journal_sha256": actual_journal,
        "raw_sha256": actual_raw,
        "source_verified_at_utc": source["verified_at_utc"],
        "status": "source-matched",
    }


class PbreadFastbootSourceVerifier:
    def __init__(self, executable: str, serial: str) -> None:
        try:
            self.transport = pbread1.FastbootTransport(executable, serial)
        except pbread1.BackupError as error:
            raise ExecuteError(str(error)) from error

    def verify(self, plan: dict[str, object], state_dir: Path) -> str:
        partition = _plan_dict(plan, "partition")
        device = _plan_dict(plan, "device")
        partuuid, partuuid32 = pbread1.normalized_uuid(str(partition["partuuid"]), "partuuid")
        parttype, parttype32 = pbread1.normalized_uuid(str(partition["type_guid"]), "type_guid")
        binding = pbread1.PartitionBinding(
            partuuid=partuuid,
            partuuid32=partuuid32,
            parttype=parttype,
            parttype32=parttype32,
            partlabel=str(partition["partlabel"]),
            kernel_name=str(partition["kernel_name_observation"]),
            start_lba=_decimal(partition["start_lba"], "start_lba"),
            sectors=_decimal(partition["sectors"], "sectors"),
            logical_sector_bytes=_decimal(partition["logical_sector_bytes"], "sector bytes"),
            partition_bytes=_decimal(partition["raw_bytes"], "raw bytes"),
        )
        manifest = {"device": {"fastboot_serial": device["fastboot_serial"]}}
        self.transport.preflight(manifest, binding)
        downloads = state_dir / ".live-source-hash"
        durable_mkdir(downloads)
        destination = downloads / f"{uuid.uuid4().hex}.pbread1"
        try:
            self.transport.stage_hash(binding, destination)
            digest = pbread1.extract_envelope(
                destination,
                None,
                binding,
                0,
                binding.partition_bytes,
                flags=pbread1.FLAG_HASH_ONLY,
            )
        except pbread1.BackupError as error:
            raise ExecuteError(f"live PBREAD1 source hash failed: {error}") from error
        finally:
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
        return f"sha256:{digest}"


def _report_rows(result: RemoteResult, section: str) -> list[dict[str, object]]:
    if result.returncode not in (0, 5):
        raise ExecuteError(
            f"read-only LVM {section} report failed with status {result.returncode}: "
            f"{result.stderr.decode(errors='replace').strip()}"
        )
    try:
        parsed = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecuteError(f"read-only LVM {section} output is not JSON: {error}") from error
    if not isinstance(parsed, dict) or not isinstance(parsed.get("report"), list):
        raise ExecuteError(f"read-only LVM {section} report has an invalid root")
    rows: list[dict[str, object]] = []
    found = False
    for report in parsed["report"]:
        if not isinstance(report, dict):
            raise ExecuteError(f"read-only LVM {section} report item is not an object")
        if section in report:
            found = True
            candidate = report[section]
            if not isinstance(candidate, list):
                raise ExecuteError(f"read-only LVM {section} rows are not an array")
            for row in candidate:
                if not isinstance(row, dict):
                    raise ExecuteError(f"read-only LVM {section} row is not an object")
                rows.append(dict(row))
    if not found:
        raise ExecuteError(f"read-only LVM output omitted the {section} report")
    if result.returncode == 5 and rows:
        raise ExecuteError(
            f"read-only LVM {section} returned no-rows status 5 with nonempty rows"
        )
    return rows


def _normalize_scalar(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _normalize_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    normalized = [
        {key: _normalize_scalar(value) for key, value in sorted(row.items())}
        for row in rows
    ]
    return sorted(normalized, key=lambda row: canonical_bytes(row))


@dataclasses.dataclass(frozen=True)
class LvmSnapshot:
    pvs: tuple[dict[str, object], ...]
    vgs: tuple[dict[str, object], ...]
    lvs: tuple[dict[str, object], ...]

    def canonical(self) -> dict[str, object]:
        return {"pvs": list(self.pvs), "vgs": list(self.vgs), "lvs": list(self.lvs)}

    def digest(self) -> str:
        return sha256_bytes(canonical_bytes(self.canonical()))


def replace_placeholders(
    argv: Sequence[str],
    *,
    device_path: str,
    state_dir: Path,
    remote_dir: str,
) -> list[str]:
    replacements = {
        bootstrap_plan.DEVICE_PLACEHOLDER: device_path,
        bootstrap_plan.HOST_STATE_DIR_PLACEHOLDER: str(state_dir),
        bootstrap_plan.REMOTE_STAGING_DIR_PLACEHOLDER: remote_dir,
    }
    result = []
    for item in argv:
        replaced = item
        for placeholder, value in replacements.items():
            replaced = replaced.replace(placeholder, value)
        if "@" in replaced and any(token in replaced for token in ("DEVICE@", "STATE_DIR@")):
            raise ExecuteError(f"unresolved plan placeholder in argv: {item!r}")
        result.append(replaced)
    return result


def read_lvm_snapshot(
    transport: ShellTransport,
    plan: dict[str, object],
    *,
    device_path: str,
    state_dir: Path,
    remote_dir: str,
) -> LvmSnapshot:
    transaction = _plan_dict(plan, "transaction")
    reports = transaction.get("verification_argv")
    if not isinstance(reports, list) or len(reports) != 3:
        raise ExecuteError("validated plan verification argv is malformed")
    results: list[RemoteResult] = []
    for index, argv in enumerate(reports):
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ExecuteError("validated plan verification argv is malformed")
        resolved = replace_placeholders(
            argv,
            device_path=device_path,
            state_dir=state_dir,
            remote_dir=remote_dir,
        )
        if index == 2:
            # Plan v1's report predates the executor and omitted the two fields
            # needed to prove lvconvert's discard/error-on-full options.  A
            # read-only superset is safe to derive; silently accepting an
            # unverifiable thin-pool policy is not.
            try:
                output_index = resolved.index("-o") + 1
            except ValueError as error:
                raise ExecuteError("validated LVM LV report lacks -o") from error
            resolved[output_index] += ",discards,lv_when_full"
        results.append(transport.run(resolved, timeout=60))
    return LvmSnapshot(
        tuple(_normalize_rows(_report_rows(results[0], "pv"))),
        tuple(_normalize_rows(_report_rows(results[1], "vg"))),
        tuple(_normalize_rows(_report_rows(results[2], "lv"))),
    )


def _tags(value: object, field: str) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, list):
        if not all(isinstance(item, str) and item.strip() for item in value):
            raise ExecuteError(f"{field} contains a malformed json_std tag")
        source = value
    elif isinstance(value, str):
        # Keep compatibility with LVM's legacy JSON mode in unit-level input;
        # production uses json_std and therefore normally takes the list arm.
        source = value.split(",")
    else:
        raise ExecuteError(f"{field} is not an LVM tag list")
    tags = tuple(sorted(item.strip() for item in source if item.strip()))
    if len(tags) != len(set(tags)):
        raise ExecuteError(f"{field} contains duplicate tags")
    return tags


def _lv_name(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ExecuteError("LVM report contains an empty lv_name")
    name = value.strip()
    if len(name) >= 2 and name[0] == "[" and name[-1] == "]":
        name = name[1:-1]
    return name


def _lvm_uuid(value: object, field: str) -> str:
    if not isinstance(value, str) or not UUID_FIELD_RE.fullmatch(value.strip()):
        raise ExecuteError(f"{field} is not an LVM UUID: {value!r}")
    return value.strip()


def _field_int(row: dict[str, object], name: str) -> int:
    if name not in row:
        raise ExecuteError(f"LVM report omitted {name}")
    return _decimal(row[name], name)


def _field_text(row: dict[str, object], name: str) -> str:
    value = row.get(name)
    if not isinstance(value, str):
        raise ExecuteError(f"LVM report omitted or malformed {name}")
    return value.strip()


def _physical_devices(value: object) -> tuple[str, ...]:
    if value in (None, "", []):
        return ()
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            raise ExecuteError("LVM devices list contains a non-string item")
        source = value
    elif isinstance(value, str):
        source = value.split(",")
    else:
        raise ExecuteError("LVM devices field is not a string list")
    result = []
    for token in source:
        item = token.strip()
        if not item:
            continue
        match = re.fullmatch(r"(.+)\([0-9]+\)", item)
        if not match:
            raise ExecuteError(f"LVM devices field contains an unrecognized extent: {item!r}")
        result.append(match.group(1))
    return tuple(result)


def _volume_plan(plan: dict[str, object]) -> list[dict[str, object]]:
    lvm = _plan_dict(plan, "lvm")
    volumes = lvm.get("volumes")
    if not isinstance(volumes, list) or not all(isinstance(item, dict) for item in volumes):
        raise ExecuteError("validated plan volume layout is malformed")
    return [dict(item) for item in volumes]


def _capacity_requirements(plan: dict[str, object]) -> tuple[int, int, int, int]:
    """Validate and return allocation, reserve, conservative capacity/free."""

    lvm = _plan_dict(plan, "lvm")
    capacity = _plan_dict(lvm, "capacity")
    partition_bytes = _decimal(
        _plan_dict(plan, "partition")["raw_bytes"], "partition bytes"
    )
    if _decimal(capacity["partition_bytes"], "capacity partition bytes") != partition_bytes:
        raise ExecuteError("capacity model is not bound to the userdata byte geometry")
    pe_bytes = _decimal(lvm["physical_extent_bytes"], "PE bytes")
    metadata_budget = _decimal(
        capacity["pv_metadata_alignment_budget_bytes"], "PV metadata budget"
    )
    conservative = _decimal(
        capacity["conservative_extent_capacity_after_budget_bytes"],
        "conservative PV capacity",
    )
    if metadata_budget != bootstrap_plan.PV_METADATA_ALIGNMENT_BUDGET:
        raise ExecuteError("capacity model changed the frozen PV metadata budget")
    if conservative != ((partition_bytes - metadata_budget) // pe_bytes) * pe_bytes:
        raise ExecuteError("capacity model's conservative PV extent capacity is inconsistent")
    volumes = _volume_plan(plan)
    planned = _decimal(capacity["planned_physical_lv_bytes"], "planned allocations")
    physical_sum = sum(_decimal(item["size_bytes"], "planned LV size") for item in volumes)
    if planned != physical_sum or planned % pe_bytes:
        raise ExecuteError("capacity model's physical allocation total is inconsistent")
    planned_free = _decimal(
        capacity["planned_free_extents_after_allocations_bytes"],
        "planned free extents",
    )
    reserve = _decimal(
        capacity["mandatory_recovery_reserve_bytes"], "mandatory recovery reserve"
    )
    slack = _decimal(
        capacity["uncommitted_slack_beyond_reserve_bytes"],
        "uncommitted recovery slack",
    )
    if reserve != 16 * 1024 * 1024 * 1024:
        raise ExecuteError("capacity model changed the mandatory 16 GiB recovery reserve")
    if planned_free != conservative - planned or planned_free != reserve + slack:
        raise ExecuteError("capacity model does not preserve its recovery reserve arithmetic")
    if slack <= 0:
        raise ExecuteError("capacity model leaves no slack beyond the recovery reserve")
    return planned, reserve, conservative, planned_free


def _validate_linear_lv(
    row: dict[str, object],
    expected: dict[str, object],
    device_path: str,
) -> None:
    name = str(expected["name"])
    if _field_int(row, "lv_size") != _decimal(expected["size_bytes"], f"{name} size"):
        raise ExecuteError(f"{name} LV size differs from the plan")
    if _field_text(row, "segtype") != "linear":
        raise ExecuteError(f"{name} is not a linear LV")
    if _tags(row.get("lv_tags"), f"{name} tags") != tuple(sorted(expected["tags"])):
        raise ExecuteError(f"{name} LV tags differ from the plan")
    if row.get("lv_active") not in (0, "0", "inactive", ""):
        raise ExecuteError(f"{name} is unexpectedly active")
    permissions = row.get("lv_permissions")
    if not isinstance(permissions, str):
        raise ExecuteError(f"{name} permissions report is malformed")
    attributes = _field_text(row, "lv_attr")
    if len(attributes) < 3 or attributes[1] != "w" or attributes[2] != "c":
        raise ExecuteError(f"{name} is not writable and contiguous in LVM metadata")
    devices = _physical_devices(row.get("devices"))
    if not devices or any(item != device_path for item in devices):
        raise ExecuteError(f"{name} has physical extents outside userdata")
    _lvm_uuid(row.get("lv_uuid"), f"{name} UUID")


def validate_lvm_stage(
    snapshot: LvmSnapshot,
    plan: dict[str, object],
    stage: int,
    device_path: str,
) -> None:
    """Require the complete fenced LVM report to equal one planned stage."""

    if not 0 <= stage <= 11:
        raise ExecuteError(f"invalid bootstrap stage: {stage}")
    if stage == 0:
        if snapshot.pvs or snapshot.vgs or snapshot.lvs:
            raise ExecuteError("stage 0 requires no PV, VG, or LV on userdata")
        return
    if len(snapshot.pvs) != 1:
        raise ExecuteError("planned stage requires exactly one userdata PV")
    pv = snapshot.pvs[0]
    lvm = _plan_dict(plan, "lvm")
    partition = _plan_dict(plan, "partition")
    (
        planned_physical_bytes,
        mandatory_reserve_bytes,
        conservative_pv_bytes,
        conservative_final_free_bytes,
    ) = _capacity_requirements(plan)
    expected_pv_uuid = str(_plan_dict(lvm, "pv")["planned_uuid"])
    if _lvm_uuid(pv.get("pv_uuid"), "PV UUID") != expected_pv_uuid:
        raise ExecuteError("userdata PV UUID is not the planned UUID")
    if _field_text(pv, "pv_name") != device_path:
        raise ExecuteError("PV report names a device other than bound userdata")
    if _field_int(pv, "dev_size") != _decimal(partition["raw_bytes"], "partition bytes"):
        raise ExecuteError("PV device size does not match userdata geometry")
    if _field_int(pv, "pv_mda_count") != 2:
        raise ExecuteError("userdata PV does not have two metadata areas")
    alignment = _decimal(_plan_dict(lvm, "pv")["data_alignment_bytes"], "PV data alignment")
    metadata_area = _decimal(_plan_dict(lvm, "pv")["metadata_area_bytes_each"], "PV metadata size")
    pe_start = _field_int(pv, "pe_start")
    if pe_start % alignment or pe_start < metadata_area or pe_start > bootstrap_plan.PV_METADATA_ALIGNMENT_BUDGET:
        raise ExecuteError("PV first-extent offset violates the planned alignment/metadata budget")
    mda_size = _field_int(pv, "pv_mda_size")
    if not metadata_area - 4096 <= mda_size <= pe_start:
        raise ExecuteError("PV metadata area size is inconsistent with the planned 16 MiB request")
    if _field_int(pv, "pv_mda_free") <= 0:
        raise ExecuteError("PV metadata areas report no free metadata space")
    if _field_int(pv, "pv_pe_alloc_count") < 0:
        raise ExecuteError("PV allocation count is invalid")
    pv_size = _field_int(pv, "pv_size")
    pv_free = _field_int(pv, "pv_free")
    if pv_size > _decimal(partition["raw_bytes"], "partition bytes"):
        raise ExecuteError("PV capacity exceeds the bound userdata partition")
    if pv_size < conservative_pv_bytes:
        raise ExecuteError(
            "PV capacity is smaller than the plan's full conservative extent capacity"
        )

    expected_pv_tags: tuple[str, ...] = () if stage < 3 else tuple(sorted(_plan_dict(lvm, "pv")["tags"]))
    if _tags(pv.get("pv_tags"), "PV tags") != expected_pv_tags:
        raise ExecuteError("userdata PV tags do not equal the planned stage")
    if stage == 1:
        if snapshot.vgs or snapshot.lvs:
            raise ExecuteError("PV-only stage unexpectedly contains a VG or LV")
        if _field_text(pv, "vg_name") or _field_text(pv, "vg_uuid"):
            raise ExecuteError("PV-only stage is unexpectedly assigned to a VG")
        if _field_int(pv, "pv_pe_alloc_count") != 0:
            raise ExecuteError("PV-only stage has allocated extents")
        if _field_int(pv, "pv_mda_used_count") != 0:
            raise ExecuteError("PV-only stage unexpectedly has in-use VG metadata areas")
        if pv_free != pv_size:
            raise ExecuteError("PV-only stage does not report its complete capacity as free")
        return

    if len(snapshot.vgs) != 1:
        raise ExecuteError("planned stage requires exactly one VG")
    vg = snapshot.vgs[0]
    if _field_int(pv, "pv_mda_used_count") != 2:
        raise ExecuteError("VG stage is not using both planned PV metadata areas")
    vg_uuid = _lvm_uuid(vg.get("vg_uuid"), "VG UUID")
    if _field_text(vg, "vg_name") != lvm["vg_name"]:
        raise ExecuteError("VG name is not franken")
    if _field_text(pv, "vg_name") != lvm["vg_name"] or _lvm_uuid(pv.get("vg_uuid"), "PV VG UUID") != vg_uuid:
        raise ExecuteError("PV and VG identity disagree")
    pe_bytes = _decimal(lvm["physical_extent_bytes"], "PE bytes")
    if _field_int(vg, "vg_extent_size") != pe_bytes:
        raise ExecuteError("VG physical extent size differs from the plan")
    if _field_int(vg, "pv_count") != 1 or _field_int(vg, "vg_missing_pv_count") != 0:
        raise ExecuteError("VG does not consist solely of the live userdata PV")
    if _tags(vg.get("vg_tags"), "VG tags") != tuple(sorted(lvm["vg_tags"])):
        raise ExecuteError("VG tags differ from the plan")
    auto = vg.get("vg_autoactivation")
    if auto not in (0, "0", "n", "no", ""):
        raise ExecuteError("VG autoactivation is not disabled")

    volumes = _volume_plan(plan)
    rows_by_name: dict[str, dict[str, object]] = {}
    for row in snapshot.lvs:
        name = _lv_name(row.get("lv_name"))
        if name in rows_by_name:
            raise ExecuteError(f"LVM report contains multiple segments for {name}")
        if _lvm_uuid(row.get("vg_uuid"), f"{name} VG UUID") != vg_uuid:
            raise ExecuteError(f"{name} belongs to an unexpected VG")
        rows_by_name[name] = row

    physical_extents = [128, 512, 2048, 64, 128, 5120]
    if 2 <= stage <= 3:
        expected_names: set[str] = set()
        allocated = 0
    elif 4 <= stage <= 9:
        count = stage - 3
        command_volumes = [*volumes[:5], volumes[6]]
        expected_names = {str(item["name"]) for item in command_volumes[:count]}
        allocated = sum(physical_extents[:count])
        for expected in command_volumes[:count]:
            _validate_linear_lv(rows_by_name.get(str(expected["name"]), {}), expected, device_path)
    else:
        expected_names = {
            "ggmeta",
            "boot-rescue",
            "home",
            "homed-state",
            "pool",
            "pool_tdata",
            "pool_tmeta",
            "lvol0_pmspare",
        }
        if stage == 11:
            expected_names.add("disk-duranium")
        allocated = 8128
        for expected in volumes[:4]:
            _validate_linear_lv(rows_by_name.get(str(expected["name"]), {}), expected, device_path)
        pool = rows_by_name.get("pool")
        tdata = rows_by_name.get("pool_tdata")
        tmeta = rows_by_name.get("pool_tmeta")
        spare = rows_by_name.get("lvol0_pmspare")
        if not all(isinstance(item, dict) for item in (pool, tdata, tmeta, spare)):
            raise ExecuteError("thin-pool stage lacks one or more exact internal LVs")
        assert pool is not None and tdata is not None and tmeta is not None and spare is not None
        if _field_text(pool, "segtype") != "thin-pool":
            raise ExecuteError("pool is not a thin-pool segment")
        if _field_int(pool, "lv_size") != _decimal(volumes[6]["size_bytes"], "pool size"):
            raise ExecuteError("thin pool size differs from the plan")
        if _field_int(pool, "chunk_size") != _decimal(_plan_dict(lvm, "thin_pool")["chunk_bytes"], "chunk size"):
            raise ExecuteError("thin pool chunk size differs from the plan")
        if _tags(pool.get("lv_tags"), "pool tags") != tuple(sorted(volumes[6]["tags"])):
            raise ExecuteError("thin pool tags differ from the plan")
        pool_attr = _field_text(pool, "lv_attr")
        if len(pool_attr) < 2 or pool_attr[0] != "t" or pool_attr[1] != "w" or pool.get("lv_active") not in (0, "0", "inactive", ""):
            raise ExecuteError("thin pool type/permission/activity attributes differ from the plan")
        if _field_text(pool, "discards") != "nopassdown":
            raise ExecuteError("thin pool discard policy differs from the plan")
        if _field_text(pool, "lv_when_full") != "error":
            raise ExecuteError("thin pool is not configured to error when full")
        for hidden, size, label in (
            (tdata, _decimal(volumes[6]["size_bytes"], "pool data size"), "pool_tdata"),
            (tmeta, _decimal(volumes[4]["size_bytes"], "pool metadata size"), "pool_tmeta"),
            (spare, _decimal(volumes[5]["size_bytes"], "pmspare size"), "lvol0_pmspare"),
        ):
            if _field_int(hidden, "lv_size") != size or _field_text(hidden, "segtype") != "linear":
                raise ExecuteError(f"{label} size/type differs from the plan")
            devices = _physical_devices(hidden.get("devices"))
            if not devices or any(item != device_path for item in devices):
                raise ExecuteError(f"{label} is not wholly placed on userdata")
            if hidden.get("lv_active") not in (0, "0", "inactive", ""):
                raise ExecuteError(f"{label} is unexpectedly active")
            if _field_int(hidden, "seg_start_pe") != 0:
                raise ExecuteError(f"{label} does not begin at LV extent zero")
            if _field_int(hidden, "seg_size_pe") * pe_bytes != size:
                raise ExecuteError(f"{label} segment geometry differs from its LV size")
            if hidden.get("metadata_devices") not in (None, "", []):
                raise ExecuteError(f"{label} unexpectedly references metadata devices")
        tdata_attr = _field_text(tdata, "lv_attr")
        tmeta_attr = _field_text(tmeta, "lv_attr")
        spare_attr = _field_text(spare, "lv_attr")
        if len(tdata_attr) < 2 or tdata_attr[0] != "T" or tdata_attr[1] != "w":
            raise ExecuteError("pool_tdata lacks the LVM thin-pool-data role attributes")
        if len(tmeta_attr) < 2 or tmeta_attr[0] != "e" or tmeta_attr[1] != "w":
            raise ExecuteError("pool_tmeta lacks the LVM pool-metadata role attributes")
        if len(spare_attr) < 2 or spare_attr[0] != "e" or spare_attr[1] != "w":
            raise ExecuteError("lvol0_pmspare lacks the LVM metadata-spare role attributes")
        if _tags(tdata.get("lv_tags"), "pool_tdata tags"):
            raise ExecuteError("pool_tdata has unexpected custom tags")
        if _tags(tmeta.get("lv_tags"), "pool_tmeta tags") != tuple(sorted(volumes[4]["tags"])):
            raise ExecuteError("pool_tmeta did not retain the planned metadata tags")
        if _tags(spare.get("lv_tags"), "pmspare tags"):
            raise ExecuteError("LVM-managed metadata spare has unexpected custom tags")
        data_uuid = _lvm_uuid(tdata.get("lv_uuid"), "pool_tdata UUID")
        meta_uuid = _lvm_uuid(tmeta.get("lv_uuid"), "pool_tmeta UUID")
        if pool.get("data_lv_uuid") != data_uuid:
            raise ExecuteError("thin pool data UUID link is inconsistent")
        if pool.get("metadata_lv_uuid") != meta_uuid:
            raise ExecuteError("thin pool metadata UUID link is inconsistent")
        if stage == 11:
            thin = rows_by_name.get("disk-duranium")
            if thin is None:
                raise ExecuteError("stage 11 lacks disk-duranium")
            if _field_text(thin, "segtype") != "thin":
                raise ExecuteError("disk-duranium is not a thin LV")
            if _field_int(thin, "lv_size") != _decimal(volumes[7]["virtual_bytes"], "Duranium virtual size"):
                raise ExecuteError("disk-duranium virtual size differs from the plan")
            if _tags(thin.get("lv_tags"), "disk-duranium tags") != tuple(sorted(volumes[7]["tags"])):
                raise ExecuteError("disk-duranium tags differ from the plan")
            thin_attr = _field_text(thin, "lv_attr")
            if len(thin_attr) < 2 or thin_attr[0] != "V" or thin_attr[1] != "w" or thin.get("lv_active") not in (0, "0", "inactive", ""):
                raise ExecuteError("disk-duranium type/permission/activity attributes differ from the plan")
            pool_uuid = _lvm_uuid(pool.get("lv_uuid"), "pool UUID")
            if thin.get("pool_lv_uuid") != pool_uuid:
                raise ExecuteError("disk-duranium points at an unexpected thin pool")

    if set(rows_by_name) != expected_names:
        raise ExecuteError(
            "complete LV set differs from the planned stage: "
            f"expected {sorted(expected_names)}, found {sorted(rows_by_name)}"
        )
    if _field_int(pv, "pv_pe_alloc_count") != allocated:
        raise ExecuteError("PV allocated extent count differs from the planned stage")
    pe_count = _field_int(pv, "pv_pe_count")
    if _field_int(vg, "vg_extent_count") != pe_count:
        raise ExecuteError("PV and VG extent counts disagree")
    if _field_int(vg, "vg_free_count") != pe_count - allocated:
        raise ExecuteError("VG free extent count differs from the planned stage")
    if _field_int(pv, "pv_free") != (pe_count - allocated) * pe_bytes:
        raise ExecuteError("PV free bytes differ from the exact free extent count")
    if _field_int(vg, "vg_free") != (pe_count - allocated) * pe_bytes:
        raise ExecuteError("VG free bytes differ from the exact free extent count")
    free_bytes = (pe_count - allocated) * pe_bytes
    if stage >= 10 and free_bytes < conservative_final_free_bytes:
        raise ExecuteError("final layout lacks the plan's full conservative free extent budget")
    if free_bytes < mandatory_reserve_bytes:
        raise ExecuteError("planned LVM stage consumes the mandatory 16 GiB reserve")
    if free_bytes < conservative_pv_bytes - allocated * pe_bytes:
        raise ExecuteError("planned LVM stage consumes conservative uncommitted slack")
    if _field_int(vg, "vg_size") != pe_count * pe_bytes:
        raise ExecuteError("VG size differs from the exact userdata PE count")
    if _field_int(pv, "pv_size") != pe_count * pe_bytes:
        raise ExecuteError("PV size differs from its exact complete-extent capacity")
    visible_lv_count = len(expected_names) if stage < 10 else 5 + (1 if stage == 11 else 0)
    if _field_int(vg, "lv_count") != visible_lv_count:
        raise ExecuteError("VG visible-LV count differs from the planned stage")


def detect_lvm_stage(snapshot: LvmSnapshot, plan: dict[str, object], device_path: str) -> int:
    matches = []
    failures = []
    for stage in range(12):
        try:
            validate_lvm_stage(snapshot, plan, stage, device_path)
        except ExecuteError as error:
            failures.append((stage, str(error)))
        else:
            matches.append(stage)
    if len(matches) != 1:
        detail = "; ".join(f"stage {stage}: {reason}" for stage, reason in failures[-3:])
        raise ExecuteError(
            f"live LVM state matches {len(matches)} planned stages, expected exactly one; {detail}"
        )
    return matches[0]


def generated_ids(snapshot: LvmSnapshot) -> dict[str, object]:
    result: dict[str, object] = {"pv_uuid": None, "vg_uuid": None, "lv_uuids": {}}
    if snapshot.pvs:
        result["pv_uuid"] = _lvm_uuid(snapshot.pvs[0].get("pv_uuid"), "PV UUID")
    if snapshot.vgs:
        result["vg_uuid"] = _lvm_uuid(snapshot.vgs[0].get("vg_uuid"), "VG UUID")
    lv_ids: dict[str, str] = {}
    for row in snapshot.lvs:
        name = _lv_name(row.get("lv_name"))
        lv_ids[name] = _lvm_uuid(row.get("lv_uuid"), f"{name} UUID")
    result["lv_uuids"] = dict(sorted(lv_ids.items()))
    return result


def snapshot_from_canonical(value: object, field: str) -> LvmSnapshot:
    if not isinstance(value, dict) or set(value) != {"pvs", "vgs", "lvs"}:
        raise ExecuteError(f"{field} is not a complete canonical LVM state")
    rows: list[tuple[dict[str, object], ...]] = []
    for section in ("pvs", "vgs", "lvs"):
        candidate = value.get(section)
        if not isinstance(candidate, list) or not all(
            isinstance(item, dict) for item in candidate
        ):
            raise ExecuteError(f"{field}.{section} is not an array of report rows")
        # Preserve report row order because it is part of the recorded digest;
        # validate_lvm_stage treats the complete rows as a set by identity.
        rows.append(tuple(dict(item) for item in candidate))
    return LvmSnapshot(rows[0], rows[1], rows[2])


def validate_uuid_continuity(
    previous: LvmSnapshot,
    current: LvmSnapshot,
    previous_ordinal: int,
    current_ordinal: int,
) -> None:
    before = generated_ids(previous)
    after = generated_ids(current)
    if before["pv_uuid"] != after["pv_uuid"]:
        raise ExecuteError(
            f"PV UUID continuity fails between checkpoints {previous_ordinal} and {current_ordinal}"
        )
    if before["vg_uuid"] is not None and before["vg_uuid"] != after["vg_uuid"]:
        raise ExecuteError(
            f"VG UUID continuity fails between checkpoints {previous_ordinal} and {current_ordinal}"
        )
    before_lvs = before["lv_uuids"]
    after_lvs = after["lv_uuids"]
    assert isinstance(before_lvs, dict) and isinstance(after_lvs, dict)
    # lvconvert changes the internal presentation at stage 10.  The four
    # stable thick LVs and every same-name LV outside that boundary retain
    # their generated identity exactly.
    stable_names = set(before_lvs) & set(after_lvs)
    if previous_ordinal == 9 and current_ordinal == 10:
        stable_names &= {"ggmeta", "boot-rescue", "home", "homed-state"}
    for name in stable_names:
        if before_lvs[name] != after_lvs[name]:
            raise ExecuteError(
                f"LV UUID continuity for {name} fails between checkpoints "
                f"{previous_ordinal} and {current_ordinal}"
            )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def durable_mkdir(path: Path, mode: int = 0o700) -> None:
    """Create a directory chain and durably publish every new directory entry."""

    missing: list[Path] = []
    cursor = path
    while True:
        try:
            status = cursor.lstat()
        except FileNotFoundError:
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                raise ExecuteError(f"cannot find an existing parent for directory {path}")
            cursor = parent
            continue
        if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
            raise ExecuteError(f"directory path has a non-directory or symlink component: {cursor}")
        break
    for directory in reversed(missing):
        parent = directory.parent
        try:
            os.mkdir(directory, mode)
        except OSError as error:
            raise ExecuteError(f"cannot create durable directory {directory}: {error}") from error
        _fsync_directory(directory)
        _fsync_directory(parent)


def atomic_write(path: Path, value: bytes, mode: int = 0o600) -> None:
    durable_mkdir(path.parent)
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, mode
        )
        try:
            offset = 0
            while offset < len(value):
                written = os.write(descriptor, value[offset:])
                if written <= 0:
                    raise ExecuteError(f"short write while persisting {path}")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def open_durable_lock_file(path: Path) -> int:
    durable_mkdir(path.parent)
    flags = os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ExecuteError(f"cannot open executor lock {path}: {error}") from error
    except OSError as error:
        raise ExecuteError(f"cannot create executor lock {path}: {error}") from error
    else:
        try:
            os.fsync(descriptor)
            _fsync_directory(path.parent)
        except Exception:
            os.close(descriptor)
            raise
    status = os.fstat(descriptor)
    if not stat.S_ISREG(status.st_mode) or status.st_uid != os.geteuid():
        os.close(descriptor)
        raise ExecuteError("executor lock is not a regular file owned by the invoking user")
    return descriptor


def write_json(path: Path, value: object) -> None:
    atomic_write(path, canonical_bytes(value))


def read_json(path: Path, field: str) -> object:
    raw = _open_regular_nofollow(path, MAX_PLAN_BYTES, field)
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ExecuteError(f"{field} is not valid JSON: {error}") from error


def write_once_or_match(path: Path, value: object, field: str) -> None:
    encoded = canonical_bytes(value)
    if path.exists():
        existing = _open_regular_nofollow(path, MAX_PLAN_BYTES, field)
        if existing != encoded:
            raise ExecuteError(f"existing {field} does not match this invocation")
        return
    write_json(path, value)


def _mount_filesystem(path: Path) -> str:
    resolved = path.resolve()
    best: tuple[int, str] | None = None
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError as error:
        raise ExecuteError(f"cannot inspect host mountinfo: {error}") from error
    for line in lines:
        fields = line.split()
        if "-" not in fields or len(fields) < 10:
            continue
        separator = fields.index("-")
        mountpoint = Path(fields[4].replace("\\040", " "))
        try:
            resolved.relative_to(mountpoint)
        except ValueError:
            continue
        candidate = (len(str(mountpoint)), fields[separator + 1])
        if best is None or candidate[0] > best[0]:
            best = candidate
    if best is None:
        raise ExecuteError("could not identify the host state directory filesystem")
    return best[1]


def prepare_state_dir(path: Path, *, require_durable: bool = True) -> Path:
    if not path.is_absolute():
        raise ExecuteError("host state directory must be an absolute path")
    durable_mkdir(path)
    resolved = path.resolve()
    if resolved != path:
        raise ExecuteError("host state directory must be canonical and contain no symlink components")
    status = resolved.stat()
    if not stat.S_ISDIR(status.st_mode):
        raise ExecuteError("host state path is not a directory")
    if status.st_uid != os.geteuid():
        raise ExecuteError("host state directory is not owned by the invoking user")
    if stat.S_IMODE(status.st_mode) & 0o077:
        raise ExecuteError("host state directory must not grant group/other permissions")
    if require_durable:
        filesystem = _mount_filesystem(resolved)
        if filesystem in VOLATILE_HOST_FILESYSTEMS:
            raise ExecuteError(
                f"host state directory is on non-durable filesystem {filesystem!r}"
            )
    _fsync_directory(resolved)
    return resolved


def prepare_pbread_run_dir(path: Path, *, require_durable: bool = True) -> Path:
    if not path.is_absolute() or path.resolve() != path:
        raise ExecuteError("PBREAD1 run directory must be an absolute canonical path")
    try:
        status = path.lstat()
    except OSError as error:
        raise ExecuteError(f"cannot inspect PBREAD1 run directory: {error}") from error
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        raise ExecuteError("PBREAD1 run path is not a real directory")
    lock_path = path / ".lock"
    try:
        lock_status = lock_path.lstat()
    except OSError as error:
        raise ExecuteError(f"cannot inspect PBREAD1 run lock: {error}") from error
    if stat.S_ISLNK(lock_status.st_mode) or not stat.S_ISREG(lock_status.st_mode):
        raise ExecuteError("PBREAD1 run lock is not a regular non-symlink file")
    if require_durable:
        filesystem = _mount_filesystem(path)
        if filesystem in VOLATILE_HOST_FILESYSTEMS:
            raise ExecuteError(
                f"PBREAD1 run directory is on non-durable filesystem {filesystem!r}"
            )
    # Besides checking that fsync is supported, this commits a pre-existing
    # run directory and lock entry before either participates in authorization.
    _fsync_directory(path)
    _fsync_directory(path.parent)
    return path


def _safe_host_capture(state_dir: Path, template: str) -> Path:
    prefix = bootstrap_plan.HOST_STATE_DIR_PLACEHOLDER
    if not template.startswith(prefix + "/"):
        raise ExecuteError("host capture path is outside the plan state placeholder")
    relative = Path(template[len(prefix) + 1 :])
    if relative.is_absolute() or ".." in relative.parts:
        raise ExecuteError("host capture path escapes the durable state directory")
    result = state_dir / relative
    if result.parent.resolve().is_relative_to(state_dir):
        return result
    raise ExecuteError("host capture parent escapes the durable state directory")


def _safe_remote_capture(remote_dir: str, template: str) -> str:
    prefix = bootstrap_plan.REMOTE_STAGING_DIR_PLACEHOLDER
    if not template.startswith(prefix + "/"):
        raise ExecuteError("remote capture path is outside the volatile state placeholder")
    suffix = template[len(prefix) :]
    if "/../" in suffix or suffix.endswith("/.."):
        raise ExecuteError("remote capture path escapes its volatile directory")
    return _safe_remote_path(remote_dir + suffix)


def _result_record(result: RemoteResult) -> dict[str, object]:
    return {
        "argv": list(result.argv),
        "returncode": result.returncode,
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_sha256": sha256_bytes(result.stderr),
        "stdout_utf8": result.stdout.decode("utf-8", errors="replace"),
        "stderr_utf8": result.stderr.decode("utf-8", errors="replace"),
    }


def _validate_vgcfg(data: bytes, snapshot: LvmSnapshot) -> dict[str, object]:
    if not data or len(data) > MAX_REMOTE_FILE_BYTES:
        raise ExecuteError("vgcfgbackup capture is empty or too large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as error:
        raise ExecuteError("vgcfgbackup capture is not ASCII") from error
    ids = generated_ids(snapshot)
    required = [
        identifier
        for identifier in (
            ids.get("pv_uuid"),
            ids.get("vg_uuid"),
            *ids["lv_uuids"].values(),
        )
        if identifier
    ]
    captured = re.findall(r'(?m)^\s*id\s*=\s*"([^"]+)"\s*$', text)
    if len(captured) != len(set(captured)):
        raise ExecuteError("vgcfgbackup contains duplicate id assignments")
    for identifier in captured:
        _lvm_uuid(identifier, "vgcfgbackup id")
    if set(captured) != set(required) or len(captured) != len(required):
        raise ExecuteError("vgcfgbackup UUID set does not exactly equal the live LVM state")
    if not re.search(r"(?m)^franken\s*\{\s*$", text):
        raise ExecuteError("vgcfgbackup does not contain the franken VG stanza")
    return {"bytes": str(len(data)), "sha256": sha256_bytes(data), "generated_ids": ids}


@dataclasses.dataclass
class ExecutorDependencies:
    identity_checker: Callable[[ShellTransport, dict[str, object]], dict[str, object]] = check_live_identity_and_geometry
    device_binding_checker: Callable[
        [ShellTransport, dict[str, object]], dict[str, object]
    ] = check_block_device_binding
    quiescence_checker: Callable[[ShellTransport, dict[str, object]], dict[str, object]] = check_quiescence
    runtime_verifier: Callable[[ShellTransport, dict[str, object]], dict[str, object]] = verify_runtime_artifacts
    snapshot_reader: Callable[..., LvmSnapshot] = read_lvm_snapshot
    backup_verifier: Callable[..., dict[str, object]] = verify_host_backup


class BootstrapExecutor:
    def __init__(
        self,
        *,
        plan: dict[str, object],
        plan_file_sha256: str,
        serial: str,
        partuuid: str,
        confirmation: str,
        recovery_attestation: str,
        pbread_run: Path,
        state_dir: Path,
        transport: ShellTransport,
        source_verifier: SourceVerifier,
        dependencies: ExecutorDependencies | None = None,
        require_durable_state: bool = True,
    ) -> None:
        self.plan = plan
        self.plan_file_sha256 = plan_file_sha256
        self.serial = _safe_serial(serial)
        self.partuuid = partuuid
        self.confirmation = confirmation
        self.recovery_attestation = recovery_attestation
        self.pbread_run = prepare_pbread_run_dir(
            pbread_run, require_durable=require_durable_state
        )
        self.state_dir = prepare_state_dir(state_dir, require_durable=require_durable_state)
        self.transport = transport
        self.source_verifier = source_verifier
        self.dependencies = dependencies or ExecutorDependencies()
        device = _plan_dict(plan, "device")
        partition = _plan_dict(plan, "partition")
        expected_confirmation = _plan_dict(plan, "confirmation")["token"]
        if self.serial != device["fastboot_serial"]:
            raise ExecuteError("explicit serial does not match the plan")
        try:
            normalized_partuuid = str(uuid.UUID(partuuid))
        except ValueError as error:
            raise ExecuteError("explicit PARTUUID is not a UUID") from error
        if normalized_partuuid != partuuid or partuuid != partition["partuuid"]:
            raise ExecuteError("explicit PARTUUID does not canonically match the plan")
        if confirmation != expected_confirmation:
            raise ExecuteError("explicit confirmation token does not match the plan")
        if recovery_attestation != RECOVERY_ATTESTATION:
            raise ExecuteError(
                f"recovery attestation must be exactly {RECOVERY_ATTESTATION}"
            )
        if transport.serial != self.serial:
            raise ExecuteError("shell transport serial does not match the explicit serial")
        self.operation_uuid = str(plan["operation_uuid"])
        self.authorization = str(plan["authorization_sha256"])
        self.remote_dir = f"{REMOTE_ROOT}/{self.operation_uuid}"
        _safe_remote_path(self.remote_dir)

    def _intent(self) -> dict[str, object]:
        return {
            "schema": "org.frankensargo.bootstrap-execution-intent/1",
            "operation_uuid": self.operation_uuid,
            "plan_file_sha256": self.plan_file_sha256,
            "authorization_sha256": self.authorization,
            "confirmation": self.confirmation,
            "serial": self.serial,
            "partuuid": self.partuuid,
            "recovery_attestation": self.recovery_attestation,
            "pbread1_run_dir": str(self.pbread_run),
            "remote_volatile_state_dir": self.remote_dir,
        }

    def _snapshot(self, identity: dict[str, object]) -> LvmSnapshot:
        return self.dependencies.snapshot_reader(
            self.transport,
            self.plan,
            device_path=str(identity["device_path"]),
            state_dir=self.state_dir,
            remote_dir=self.remote_dir,
        )

    def _checkpoint_path(self, command: dict[str, object]) -> Path:
        checkpoint = command["checkpoint"]
        assert isinstance(checkpoint, dict)
        return _safe_host_capture(self.state_dir, str(checkpoint["state_file"]))

    def _resolved_command_argv(
        self, command: dict[str, object], identity: dict[str, object]
    ) -> list[str]:
        argv = command.get("argv")
        if not isinstance(argv, list) or not all(isinstance(item, str) for item in argv):
            raise ExecuteError("validated command argv is malformed")
        return replace_placeholders(
            argv,
            device_path=str(identity["device_path"]),
            state_dir=self.state_dir,
            remote_dir=self.remote_dir,
        )

    def _validate_command_intent(
        self,
        command: dict[str, object],
        ordinal: int,
        identity: dict[str, object],
        *,
        role: str,
    ) -> dict[str, object]:
        step = str(command["step"])
        directory = self.state_dir / "events" / f"{ordinal:02d}-{step}"
        if not directory.is_dir() or directory.is_symlink():
            raise ExecuteError(f"step {ordinal} has no durable command-event directory")
        paths = sorted(directory.glob(f"{role}-*-intent.json"))
        if not paths:
            raise ExecuteError(
                f"step {ordinal} has no durable {role} invocation intent; refusing recovery"
            )
        if ordinal == 1 and role == "main" and len(paths) != 1:
            raise ExecuteError("pvcreate history contains more than one invocation intent")
        wanted_argv = self._resolved_command_argv(command, identity)
        latest: dict[str, object] | None = None
        latest_outcome_kind: str | None = None
        for attempt, path in enumerate(paths, start=1):
            if path.name != f"{role}-{attempt:04d}-intent.json":
                raise ExecuteError(f"step {ordinal} command intents are not contiguous")
            value = read_json(path, f"step {ordinal} command intent")
            expected_keys = {
                "schema",
                "authorization_sha256",
                "operation_uuid",
                "ordinal",
                "step",
                "role",
                "attempt",
                "argv",
                "argv_sha256",
            }
            if not isinstance(value, dict) or set(value) != expected_keys:
                raise ExecuteError(f"step {ordinal} command intent body is incomplete")
            if (
                value["schema"] != "org.frankensargo.bootstrap-command-intent/1"
                or value["authorization_sha256"] != self.authorization
                or value["operation_uuid"] != self.operation_uuid
                or value["ordinal"] != ordinal
                or value["step"] != step
                or value["role"] != role
                or value["attempt"] != attempt
                or value["argv"] != wanted_argv
                or value["argv_sha256"] != sha256_bytes(canonical_bytes(wanted_argv))
            ):
                raise ExecuteError(f"step {ordinal} command intent does not bind the plan argv")
            latest = value
            prefix = directory / f"{role}-{attempt:04d}"
            outcome_paths = {
                "result": prefix.with_name(prefix.name + "-result.json"),
                "exception": prefix.with_name(prefix.name + "-exception.json"),
                "oversized": prefix.with_name(prefix.name + "-oversized-result.json"),
            }
            present = [
                (kind, outcome_path)
                for kind, outcome_path in outcome_paths.items()
                if outcome_path.exists()
            ]
            if len(present) != 1:
                raise ExecuteError(
                    f"step {ordinal} command intent lacks one unambiguous durable outcome"
                )
            kind, outcome_path = present[0]
            outcome = read_json(outcome_path, f"step {ordinal} command outcome")
            common = {
                "authorization_sha256": self.authorization,
                "operation_uuid": self.operation_uuid,
                "ordinal": ordinal,
                "step": step,
                "role": role,
                "attempt": attempt,
                "intent_sha256": sha256_bytes(canonical_bytes(value)),
            }
            schemas = {
                "result": "org.frankensargo.bootstrap-command-result/1",
                "exception": "org.frankensargo.bootstrap-command-exception/1",
                "oversized": "org.frankensargo.bootstrap-command-oversized-result/1",
            }
            extra_keys = {
                "result": {
                    "argv",
                    "returncode",
                    "stdout_sha256",
                    "stderr_sha256",
                    "stdout_utf8",
                    "stderr_utf8",
                },
                "exception": {"exception"},
                "oversized": {
                    "returncode",
                    "stdout_bytes",
                    "stderr_bytes",
                    "stdout_sha256",
                    "stderr_sha256",
                },
            }
            if (
                not isinstance(outcome, dict)
                or set(outcome) != {"schema", *common, *extra_keys[kind]}
                or outcome.get("schema") != schemas[kind]
                or any(outcome.get(key) != wanted for key, wanted in common.items())
            ):
                raise ExecuteError(f"step {ordinal} command outcome does not bind its intent")
            if kind == "result" and outcome.get("argv") != wanted_argv:
                raise ExecuteError(f"step {ordinal} result argv differs from its intent")
            if kind == "result":
                returncode = outcome.get("returncode")
                if (
                    not isinstance(returncode, int)
                    or isinstance(returncode, bool)
                    or returncode != 0
                ):
                    raise ExecuteError(
                        f"step {ordinal} has durable nonzero remote status "
                        f"{returncode!r}; manual forensics required"
                    )
            for digest_field in ("stdout_sha256", "stderr_sha256"):
                if kind != "exception" and not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", str(outcome.get(digest_field, ""))
                ):
                    raise ExecuteError(f"step {ordinal} outcome has a malformed digest")
            latest_outcome_kind = kind
        assert latest is not None
        if latest_outcome_kind != "result":
            raise ExecuteError(
                f"step {ordinal} lacks a trustworthy remote status 0; "
                "manual forensics required"
            )
        return latest

    def _validate_main_intent(
        self,
        command: dict[str, object],
        ordinal: int,
        identity: dict[str, object],
    ) -> dict[str, object]:
        return self._validate_command_intent(
            command, ordinal, identity, role="main"
        )

    def _validate_first_write_evidence(
        self,
        event_intent: dict[str, object],
    ) -> None:
        prewrite_path = self.state_dir / "preflight/prewrite.json"
        prewrite_raw = _open_regular_nofollow(
            prewrite_path, MAX_PLAN_BYTES, "pre-write attestation"
        )
        try:
            prewrite = json.loads(prewrite_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExecuteError(f"pre-write attestation is not valid JSON: {error}") from error
        expected_prewrite_keys = {
            "schema",
            "authorization_sha256",
            "operation_uuid",
            "identity",
            "quiescence",
            "runtime",
            "host_backup",
            "live_source_sha256",
        }
        partition = _plan_dict(self.plan, "partition")
        if (
            not isinstance(prewrite, dict)
            or set(prewrite) != expected_prewrite_keys
            or prewrite.get("schema") != "org.frankensargo.bootstrap-prewrite/1"
            or prewrite.get("authorization_sha256") != self.authorization
            or prewrite.get("operation_uuid") != self.operation_uuid
            or prewrite.get("live_source_sha256")
            != partition["current_full_source_sha256"]
            or not isinstance(prewrite.get("identity"), dict)
            or prewrite["identity"].get("device_path") != TARGET_DEVICE_PATH
            or prewrite["identity"].get("parent_kernel_name") != TARGET_DISK_NAME
            or prewrite["identity"].get("kernel_name") != TARGET_PARTITION_NAME
            or prewrite["identity"].get("partuuid") != self.partuuid
        ):
            raise ExecuteError("pre-write source attestation is incomplete or misbound")
        first_intent_path = self.state_dir / "preflight/first-write-intent.json"
        first_intent = read_json(first_intent_path, "first-write intent")
        expected_intent_keys = {
            "schema",
            "authorization_sha256",
            "operation_uuid",
            "pbread1_run_dir",
            "prewrite_sha256",
            "event_intent",
            "event_intent_sha256",
        }
        expected_event_path = (
            f"events/01-{self.plan['transaction']['command_argv'][0]['step']}/"
            "main-0001-intent.json"
        )
        event_digest = sha256_bytes(canonical_bytes(event_intent))
        if (
            not isinstance(first_intent, dict)
            or set(first_intent) != expected_intent_keys
            or first_intent.get("schema")
            != "org.frankensargo.bootstrap-first-write-intent/1"
            or first_intent.get("authorization_sha256") != self.authorization
            or first_intent.get("operation_uuid") != self.operation_uuid
            or first_intent.get("pbread1_run_dir") != str(self.pbread_run)
            or first_intent.get("prewrite_sha256") != sha256_bytes(prewrite_raw)
            or first_intent.get("event_intent") != expected_event_path
            or first_intent.get("event_intent_sha256") != event_digest
        ):
            raise ExecuteError("first-write intent is incomplete or does not bind preflight")
        first_outcome = read_json(
            self.state_dir / "preflight/first-write-outcome.json",
            "first-write outcome",
        )
        expected_outcome_keys = {
            "schema",
            "authorization_sha256",
            "operation_uuid",
            "event_intent_sha256",
            "outcome_kind",
            "detail",
        }
        if (
            not isinstance(first_outcome, dict)
            or set(first_outcome) != expected_outcome_keys
            or first_outcome.get("schema")
            != "org.frankensargo.bootstrap-first-write-outcome/1"
            or first_outcome.get("authorization_sha256") != self.authorization
            or first_outcome.get("operation_uuid") != self.operation_uuid
            or first_outcome.get("event_intent_sha256") != event_digest
            or first_outcome.get("outcome_kind")
            not in {"remote-result", "transport-exception", "oversized-result"}
            or not isinstance(first_outcome.get("detail"), dict)
        ):
            raise ExecuteError("first-write outcome is incomplete or does not bind its intent")

    def _validate_checkpoint_capture(
        self,
        command: dict[str, object],
        ordinal: int,
        snapshot: LvmSnapshot,
        value: object,
    ) -> None:
        checkpoint = command["checkpoint"]
        assert isinstance(checkpoint, dict)
        planned = checkpoint.get("remote_capture")
        if planned is None:
            if value is not None:
                raise ExecuteError(f"step {ordinal} unexpectedly records a vgcfgbackup")
            return
        if not isinstance(planned, dict) or not isinstance(value, dict):
            raise ExecuteError(f"step {ordinal} lacks its planned vgcfgbackup evidence")
        if set(value) != {
            "bytes",
            "sha256",
            "generated_ids",
            "remote_source",
            "host_destination",
        }:
            raise ExecuteError(f"step {ordinal} vgcfgbackup evidence is incomplete")
        source = _safe_remote_capture(self.remote_dir, str(planned["source"]))
        destination = _safe_host_capture(
            self.state_dir, str(planned["host_destination"])
        )
        data = _open_regular_nofollow(
            destination, MAX_REMOTE_FILE_BYTES, f"step {ordinal} host vgcfgbackup"
        )
        observed = {
            **_validate_vgcfg(data, snapshot),
            "remote_source": source,
            "host_destination": str(destination),
        }
        if value != observed:
            raise ExecuteError(f"step {ordinal} vgcfgbackup file/evidence no longer match")

    def _validate_checkpoint(
        self,
        command: dict[str, object],
        ordinal: int,
        identity: dict[str, object],
    ) -> tuple[LvmSnapshot, dict[str, object]]:
        path = self._checkpoint_path(command)
        value = read_json(path, f"step {ordinal} checkpoint")
        expected_keys = {
            "schema",
            "authorization_sha256",
            "operation_uuid",
            "ordinal",
            "stage",
            "step",
            "recovered_exact_postcondition",
            "lvm_state_sha256",
            "lvm_state",
            "generated_ids",
            "vgcfgbackup",
        }
        target_stage = min(ordinal, 11)
        if (
            not isinstance(value, dict)
            or set(value) != expected_keys
            or value.get("schema") != "org.frankensargo.bootstrap-step/1"
            or value.get("authorization_sha256") != self.authorization
            or value.get("operation_uuid") != self.operation_uuid
            or value.get("ordinal") != ordinal
            or value.get("stage") != target_stage
            or value.get("step") != command["step"]
            or not isinstance(value.get("recovered_exact_postcondition"), bool)
        ):
            raise ExecuteError(f"step {ordinal} checkpoint body/binding is invalid")
        stored = snapshot_from_canonical(value["lvm_state"], f"step {ordinal} lvm_state")
        validate_lvm_stage(stored, self.plan, target_stage, TARGET_DEVICE_PATH)
        if value.get("lvm_state_sha256") != stored.digest():
            raise ExecuteError(f"step {ordinal} checkpoint LVM-state hash is invalid")
        if value.get("generated_ids") != generated_ids(stored):
            raise ExecuteError(f"step {ordinal} checkpoint generated UUIDs are incomplete")
        intent = self._validate_main_intent(command, ordinal, identity)
        self._validate_checkpoint_capture(
            command, ordinal, stored, value.get("vgcfgbackup")
        )
        checkpoint = command["checkpoint"]
        assert isinstance(checkpoint, dict)
        backup_argv = checkpoint.get("vgcfgbackup_argv")
        if backup_argv is not None:
            if not isinstance(backup_argv, list):
                raise ExecuteError(f"step {ordinal} vgcfgbackup argv is malformed")
            self._validate_command_intent(
                {"step": command["step"], "argv": backup_argv},
                ordinal,
                identity,
                role="vgcfgbackup",
            )
        return stored, intent

    def _validate_existing_checkpoints(
        self,
        commands: list[dict[str, object]],
        current_stage: int,
        snapshot: LvmSnapshot,
        identity: dict[str, object],
    ) -> None:
        expected_paths = {self._checkpoint_path(command) for command in commands}
        steps_dir = self.state_dir / "steps"
        if steps_dir.exists():
            if not steps_dir.is_dir() or steps_dir.is_symlink():
                raise ExecuteError("checkpoint path is not a real directory")
            unexpected = {
                path for path in steps_dir.glob("*.json") if path not in expected_paths
            }
            if unexpected:
                raise ExecuteError("checkpoint directory contains unplanned JSON history")

        previous: LvmSnapshot | None = None
        first_intent: dict[str, object] | None = None
        latest_stored: LvmSnapshot | None = None
        for ordinal, command in enumerate(commands[:11], start=1):
            path = self._checkpoint_path(command)
            if ordinal > current_stage:
                if path.exists():
                    raise ExecuteError(f"future checkpoint exists unexpectedly: {path}")
                continue
            if not path.exists():
                if ordinal != current_stage:
                    raise ExecuteError(
                        f"live LVM stage {current_stage} lacks checkpoint {path.name}"
                    )
                # Only the latest live postcondition may lack a checkpoint,
                # and it must have the exact durable command intent/outcome.
                stored = snapshot
                intent = self._validate_main_intent(command, ordinal, identity)
            else:
                stored, intent = self._validate_checkpoint(
                    command, ordinal, identity
                )
            if ordinal == 1:
                first_intent = intent
            if previous is not None:
                validate_uuid_continuity(previous, stored, ordinal - 1, ordinal)
            previous = stored
            latest_stored = stored

        final = self._checkpoint_path(commands[11])
        if final.exists():
            if current_stage != 11 or not self._checkpoint_path(commands[10]).exists():
                raise ExecuteError("final checkpoint exists without complete prior history")
            final_snapshot, _ = self._validate_checkpoint(commands[11], 12, identity)
            assert latest_stored is not None
            validate_uuid_continuity(latest_stored, final_snapshot, 11, 12)
            latest_stored = final_snapshot

        if current_stage >= 1:
            assert first_intent is not None and latest_stored is not None
            self._validate_first_write_evidence(first_intent)
            if latest_stored.digest() != snapshot.digest():
                raise ExecuteError("current LVM state differs from durable checkpoint history")

    def _record_prewrite(
        self,
        identity: dict[str, object],
        quiescence: dict[str, object],
        runtime: dict[str, object],
        host_backup: dict[str, object],
    ) -> None:
        expected = str(_plan_dict(self.plan, "partition")["current_full_source_sha256"])
        observed = self.source_verifier.verify(self.plan, self.state_dir)
        if observed != expected:
            raise ExecuteError(
                f"live source hash {observed} does not match backup-bound hash {expected}"
            )
        record = {
            "schema": "org.frankensargo.bootstrap-prewrite/1",
            "authorization_sha256": self.authorization,
            "operation_uuid": self.operation_uuid,
            "identity": identity,
            "quiescence": quiescence,
            "runtime": runtime,
            "host_backup": host_backup,
            "live_source_sha256": observed,
        }
        write_once_or_match(
            self.state_dir / "preflight/prewrite.json",
            record,
            "pre-write attestation",
        )

    def _event_dir(self, ordinal: int, step: str) -> Path:
        directory = self.state_dir / "events" / f"{ordinal:02d}-{step}"
        durable_mkdir(directory)
        return directory

    def _first_write_intent(self, event_path: Path, event: dict[str, object]) -> None:
        prewrite_path = self.state_dir / "preflight/prewrite.json"
        prewrite = _open_regular_nofollow(
            prewrite_path, MAX_PLAN_BYTES, "pre-write attestation"
        )
        record = {
            "schema": "org.frankensargo.bootstrap-first-write-intent/1",
            "authorization_sha256": self.authorization,
            "operation_uuid": self.operation_uuid,
            "pbread1_run_dir": str(self.pbread_run),
            "prewrite_sha256": sha256_bytes(prewrite),
            "event_intent": str(event_path.relative_to(self.state_dir)),
            "event_intent_sha256": sha256_bytes(canonical_bytes(event)),
        }
        write_once_or_match(
            self.state_dir / "preflight/first-write-intent.json",
            record,
            "first-write intent",
        )

    def _first_write_outcome(
        self,
        event: dict[str, object],
        outcome_kind: str,
        detail: dict[str, object],
    ) -> None:
        record = {
            "schema": "org.frankensargo.bootstrap-first-write-outcome/1",
            "authorization_sha256": self.authorization,
            "operation_uuid": self.operation_uuid,
            "event_intent_sha256": sha256_bytes(canonical_bytes(event)),
            "outcome_kind": outcome_kind,
            "detail": detail,
        }
        write_once_or_match(
            self.state_dir / "preflight/first-write-outcome.json",
            record,
            "first-write outcome",
        )

    def _run_argv(
        self,
        *,
        ordinal: int,
        step: str,
        role: str,
        argv: list[str],
        prevent_replay: bool = False,
    ) -> RemoteResult:
        directory = self._event_dir(ordinal, step)
        attempts = sorted(directory.glob(f"{role}-*-intent.json"))
        if prevent_replay and attempts:
            raise ExecuteError(
                "pvcreate was previously invoked without a durable exact postcondition; "
                "refusing to replay -ff"
            )
        for prior_intent in attempts:
            prefix = prior_intent.with_name(prior_intent.name.removesuffix("-intent.json"))
            prior_result = prefix.with_name(prefix.name + "-result.json")
            prior_oversized = prefix.with_name(prefix.name + "-oversized-result.json")
            if prior_oversized.exists():
                raise ExecuteError(
                    f"step {ordinal} has an oversized prior remote result; "
                    "manual forensics required"
                )
            if prior_result.exists():
                value = read_json(prior_result, f"step {ordinal} prior command result")
                if not isinstance(value, dict) or value.get("returncode") != 0:
                    raise ExecuteError(
                        f"step {ordinal} has a prior nonzero or corrupt remote result; "
                        "manual forensics required"
                    )
        attempt = len(attempts) + 1
        prefix = directory / f"{role}-{attempt:04d}"
        intent_path = prefix.with_name(prefix.name + "-intent.json")
        intent = {
            "schema": "org.frankensargo.bootstrap-command-intent/1",
            "authorization_sha256": self.authorization,
            "operation_uuid": self.operation_uuid,
            "ordinal": ordinal,
            "step": step,
            "role": role,
            "attempt": attempt,
            "argv": argv,
            "argv_sha256": sha256_bytes(canonical_bytes(argv)),
        }
        write_json(intent_path, intent)
        if ordinal == 1 and role == "main":
            self._first_write_intent(intent_path, intent)
        try:
            result = self.transport.run(argv, timeout=300)
        except Exception as error:
            detail = {"exception": f"{type(error).__name__}: {error}"}
            if ordinal == 1 and role == "main":
                self._first_write_outcome(intent, "transport-exception", detail)
            write_json(
                prefix.with_name(prefix.name + "-exception.json"),
                {
                    "schema": "org.frankensargo.bootstrap-command-exception/1",
                    "authorization_sha256": self.authorization,
                    "operation_uuid": self.operation_uuid,
                    "ordinal": ordinal,
                    "step": step,
                    "role": role,
                    "attempt": attempt,
                    "intent_sha256": sha256_bytes(canonical_bytes(intent)),
                    **detail,
                },
            )
            raise
        if len(result.stdout) > 1024 * 1024 or len(result.stderr) > 1024 * 1024:
            detail = {
                "returncode": result.returncode,
                "stdout_bytes": len(result.stdout),
                "stderr_bytes": len(result.stderr),
                "stdout_sha256": sha256_bytes(result.stdout),
                "stderr_sha256": sha256_bytes(result.stderr),
            }
            if ordinal == 1 and role == "main":
                self._first_write_outcome(intent, "oversized-result", detail)
            write_json(
                prefix.with_name(prefix.name + "-oversized-result.json"),
                {
                    "schema": "org.frankensargo.bootstrap-command-oversized-result/1",
                    "authorization_sha256": self.authorization,
                    "operation_uuid": self.operation_uuid,
                    "ordinal": ordinal,
                    "step": step,
                    "role": role,
                    "attempt": attempt,
                    "intent_sha256": sha256_bytes(canonical_bytes(intent)),
                    **detail,
                },
            )
            raise ExecuteError(f"step {ordinal} produced an unexpectedly large remote result")
        detail = _result_record(result)
        if ordinal == 1 and role == "main":
            self._first_write_outcome(intent, "remote-result", detail)
        write_json(
            prefix.with_name(prefix.name + "-result.json"),
            {
                "schema": "org.frankensargo.bootstrap-command-result/1",
                "authorization_sha256": self.authorization,
                "operation_uuid": self.operation_uuid,
                "ordinal": ordinal,
                "step": step,
                "role": role,
                "attempt": attempt,
                "intent_sha256": sha256_bytes(canonical_bytes(intent)),
                **detail,
            },
        )
        return result

    def _capture_vgcfg(
        self,
        command: dict[str, object],
        snapshot: LvmSnapshot,
        identity: dict[str, object],
        *,
        main_already_wrote_capture: bool,
    ) -> dict[str, object] | None:
        checkpoint = command["checkpoint"]
        assert isinstance(checkpoint, dict)
        capture = checkpoint.get("remote_capture")
        if capture is None:
            return None
        if not isinstance(capture, dict):
            raise ExecuteError("validated remote capture is malformed")
        ordinal = int(checkpoint["ordinal"])
        step = str(command["step"])
        if not main_already_wrote_capture:
            backup = checkpoint.get("vgcfgbackup_argv")
            if not isinstance(backup, list):
                raise ExecuteError("post-mutation vgcfgbackup argv is absent")
            argv = replace_placeholders(
                backup,
                device_path=str(identity["device_path"]),
                state_dir=self.state_dir,
                remote_dir=self.remote_dir,
            )
            result = self._run_argv(
                ordinal=ordinal,
                step=step,
                role="vgcfgbackup",
                argv=argv,
            )
            if result.returncode != 0:
                raise ExecuteError(f"step {ordinal} vgcfgbackup failed")
        source = _safe_remote_capture(self.remote_dir, str(capture["source"]))
        destination = _safe_host_capture(self.state_dir, str(capture["host_destination"]))
        data = self.transport.read_file(source, maximum=MAX_REMOTE_FILE_BYTES)
        evidence = _validate_vgcfg(data, snapshot)
        atomic_write(destination, data)
        if sha256_bytes(_open_regular_nofollow(destination, MAX_REMOTE_FILE_BYTES, "host vgcfgbackup")) != evidence["sha256"]:
            raise ExecuteError("durable host vgcfgbackup changed after fsync")
        return {**evidence, "remote_source": source, "host_destination": str(destination)}

    def _write_checkpoint(
        self,
        command: dict[str, object],
        snapshot: LvmSnapshot,
        capture: dict[str, object] | None,
        *,
        recovered_postcondition: bool,
    ) -> None:
        checkpoint = command["checkpoint"]
        assert isinstance(checkpoint, dict)
        ordinal = int(checkpoint["ordinal"])
        value = {
            "schema": "org.frankensargo.bootstrap-step/1",
            "authorization_sha256": self.authorization,
            "operation_uuid": self.operation_uuid,
            "ordinal": ordinal,
            "stage": min(ordinal, 11),
            "step": command["step"],
            "recovered_exact_postcondition": recovered_postcondition,
            "lvm_state_sha256": snapshot.digest(),
            "lvm_state": snapshot.canonical(),
            "generated_ids": generated_ids(snapshot),
            "vgcfgbackup": capture,
        }
        write_once_or_match(
            self._checkpoint_path(command),
            value,
            f"step {ordinal} checkpoint",
        )

    def execute(self, *, stop_after_step: int | None = None) -> dict[str, object]:
        lock_path = self.state_dir / "executor.lock"
        lock_descriptor = open_durable_lock_file(lock_path)
        run_lock: contextlib.AbstractContextManager[None] | None = None
        run_lock_entered = False
        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ExecuteError("another bootstrap executor holds the state lock") from error
            run_lock = pbread1.run_lock(self.pbread_run, exclusive=False)
            try:
                run_lock.__enter__()
                run_lock_entered = True
            except (OSError, pbread1.BackupError) as error:
                raise ExecuteError(f"could not hold PBREAD1 run lock: {error}") from error
            write_once_or_match(self.state_dir / "intent.json", self._intent(), "execution intent")
            host_backup = self.dependencies.backup_verifier(
                self.plan, self.pbread_run, lock_held=True
            )
            write_once_or_match(
                self.state_dir / "preflight/host-backup.json",
                {
                    "authorization_sha256": self.authorization,
                    "operation_uuid": self.operation_uuid,
                    **host_backup,
                },
                "host backup attestation",
            )
            identity = self.dependencies.identity_checker(self.transport, self.plan)
            if (
                identity.get("partuuid") != self.partuuid
                or identity.get("kernel_name") != TARGET_PARTITION_NAME
                or identity.get("parent_kernel_name") != TARGET_DISK_NAME
                or identity.get("device_path") != TARGET_DEVICE_PATH
            ):
                raise ExecuteError("live identity checker returned a different target binding")
            self.dependencies.device_binding_checker(self.transport, identity)
            quiescence = self.dependencies.quiescence_checker(self.transport, identity)
            runtime = self.dependencies.runtime_verifier(self.transport, self.plan)
            mkdir = self.transport.run(["/bin/mkdir", "-p", self.remote_dir + "/steps"], timeout=30)
            if mkdir.returncode != 0:
                raise ExecuteError("could not prepare PocketBoot volatile transaction directory")

            transaction = _plan_dict(self.plan, "transaction")
            raw_commands = transaction.get("command_argv")
            if not isinstance(raw_commands, list) or not all(isinstance(item, dict) for item in raw_commands):
                raise ExecuteError("validated plan command list is malformed")
            commands = [dict(item) for item in raw_commands]
            snapshot = self._snapshot(identity)
            current_stage = detect_lvm_stage(snapshot, self.plan, str(identity["device_path"]))
            self._validate_existing_checkpoints(
                commands, current_stage, snapshot, identity
            )
            if current_stage == 0:
                self._record_prewrite(identity, quiescence, runtime, host_backup)
            if stop_after_step == 0:
                return {
                    "operation_uuid": self.operation_uuid,
                    "authorization_sha256": self.authorization,
                    "serial": self.serial,
                    "partuuid": self.partuuid,
                    "stage": current_stage,
                    "bootstrap_complete": self._checkpoint_path(commands[11]).exists(),
                    "preflight_only": True,
                    "state_dir": str(self.state_dir),
                    "generated_ids": generated_ids(snapshot),
                }

            for ordinal, command in enumerate(commands, start=1):
                checkpoint = command.get("checkpoint")
                if not isinstance(checkpoint, dict) or checkpoint.get("ordinal") != ordinal:
                    raise ExecuteError("validated command checkpoint ordinal is inconsistent")
                target_stage = min(ordinal, 11)
                checkpoint_path = self._checkpoint_path(command)
                if checkpoint_path.exists():
                    if ordinal <= current_stage or (ordinal == 12 and current_stage == 11):
                        if stop_after_step == ordinal:
                            break
                        continue
                    raise ExecuteError(f"future checkpoint exists unexpectedly: {checkpoint_path}")

                recovered = current_stage == target_stage and ordinal <= 11
                if ordinal == 12 and current_stage == 11:
                    recovered = False
                if not recovered:
                    expected_before = ordinal - 1 if ordinal <= 11 else 11
                    if current_stage != expected_before:
                        raise ExecuteError(
                            f"step {ordinal} requires stage {expected_before}, live stage is {current_stage}"
                        )
                    validate_lvm_stage(snapshot, self.plan, expected_before, str(identity["device_path"]))
                    # Recheck the live no-use gates immediately before every
                    # planned argv, not merely once at process startup.
                    self.dependencies.device_binding_checker(self.transport, identity)
                    self.dependencies.quiescence_checker(self.transport, identity)
                    argv = self._resolved_command_argv(command, identity)
                    result = self._run_argv(
                        ordinal=ordinal,
                        step=str(command["step"]),
                        role="main",
                        argv=argv,
                        prevent_replay=ordinal == 1,
                    )
                    post = self._snapshot(identity)
                    if result.returncode != 0:
                        write_once_or_match(
                            self._event_dir(ordinal, str(command["step"]))
                            / "post-nonzero-state.json",
                            {
                                "authorization_sha256": self.authorization,
                                "operation_uuid": self.operation_uuid,
                                "ordinal": ordinal,
                                "returncode": result.returncode,
                                "lvm_state_sha256": post.digest(),
                                "lvm_state": post.canonical(),
                            },
                            f"step {ordinal} nonzero-result live state",
                        )
                        raise ExecuteError(
                            f"step {ordinal} returned trustworthy remote status "
                            f"{result.returncode}; manual forensics required"
                        )
                    try:
                        validate_lvm_stage(post, self.plan, target_stage, str(identity["device_path"]))
                    except ExecuteError:
                        # Preserve a failed command's exact observed state.  A
                        # later invocation may retry non-pvcreate steps only if
                        # it still equals the exact precondition.
                        write_json(
                            self._event_dir(ordinal, str(command["step"])) / "post-failure-state.json",
                            {"lvm_state_sha256": post.digest(), "lvm_state": post.canonical()},
                        )
                        if result.returncode != 0:
                            raise ExecuteError(
                                f"step {ordinal} returned {result.returncode} and did not reach its exact postcondition"
                            )
                        raise
                    snapshot = post
                    current_stage = target_stage
                    write_json(
                        self._event_dir(ordinal, str(command["step"])) / "post-main-state.json",
                        {"lvm_state_sha256": snapshot.digest(), "lvm_state": snapshot.canonical()},
                    )
                else:
                    validate_lvm_stage(snapshot, self.plan, target_stage, str(identity["device_path"]))

                capture = self._capture_vgcfg(
                    command,
                    snapshot,
                    identity,
                    main_already_wrote_capture=ordinal == 12 and not recovered,
                )
                stable = self._snapshot(identity)
                validate_lvm_stage(stable, self.plan, target_stage, str(identity["device_path"]))
                if stable.digest() != snapshot.digest():
                    raise ExecuteError("LVM state changed while vgcfg/checkpoint evidence was captured")
                snapshot = stable
                self._write_checkpoint(
                    command,
                    snapshot,
                    capture,
                    recovered_postcondition=recovered,
                )
                if stop_after_step == ordinal:
                    break

            complete = self._checkpoint_path(commands[11]).exists()
            return {
                "operation_uuid": self.operation_uuid,
                "authorization_sha256": self.authorization,
                "serial": self.serial,
                "partuuid": self.partuuid,
                "stage": current_stage,
                "bootstrap_complete": complete,
                "state_dir": str(self.state_dir),
                "generated_ids": generated_ids(snapshot),
            }
        finally:
            if run_lock is not None and run_lock_entered:
                run_lock.__exit__(None, None, None)
            os.close(lock_descriptor)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execute-bootstrap",
        description=(
            "Execute one exact userdata-anchor bootstrap-plan v1 transaction "
            "over a PocketBoot shell with durable fail-closed checkpoints."
        ),
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--partuuid", required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--recovery-attestation", required=True)
    parser.add_argument("--pbread-run", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--adb", default=os.environ.get("ADB", "adb"))
    parser.add_argument("--fastboot", default=os.environ.get("FASTBOOT", "fastboot"))
    parser.add_argument(
        "--stop-after-step",
        type=int,
        choices=range(0, 13),
        help="0 performs all read-only gates and no LVM mutation; 1-12 pause after that checkpoint",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="required arming flag; without it no device command is attempted",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.execute:
        print("refusing to contact a device without the explicit --execute arming flag", file=sys.stderr)
        return 2
    try:
        plan, plan_file_sha256 = load_and_validate_plan(args.plan)
        transport = AdbShellTransport(args.adb, args.serial)
        source_verifier = PbreadFastbootSourceVerifier(args.fastboot, args.serial)
        executor = BootstrapExecutor(
            plan=plan,
            plan_file_sha256=plan_file_sha256,
            serial=args.serial,
            partuuid=args.partuuid,
            confirmation=args.confirm,
            recovery_attestation=args.recovery_attestation,
            pbread_run=args.pbread_run,
            state_dir=args.state_dir,
            transport=transport,
            source_verifier=source_verifier,
        )
        result = executor.execute(stop_after_step=args.stop_after_step)
    except ExecuteError as error:
        print(f"execute-bootstrap: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
