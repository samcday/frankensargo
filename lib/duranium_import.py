#!/usr/bin/env python3
"""Crash-resumable, identity-bound Duranium sparse-import controller.

The transaction core is transport-independent so its crash windows can be
tested without a phone.  The production transport deliberately requires ADB
shell_v2: legacy PocketBoot ADB cannot report a trustworthy remote child exit
status and is therefore rejected before activation or writes.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import decimal
import fcntl
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import BinaryIO, Callable, Protocol, Sequence

from adb_shell_v2 import AdbShellV2, ShellV2Error


SCHEMA = "org.frankensargo.duranium-import-transaction/1"
AUDIT_SCHEMA = "org.frankensargo.duranium-import-audit/1"
POCKETBOOT_PROVENANCE_SCHEMA = "org.frankensargo.pocketboot-bound-image/1"
CHECKPOINT_SCHEMA = "org.frankensargo.bootstrap-step/1"
PLAN_SCHEMA = "org.frankensargo.bootstrap-plan/1"
JOURNAL_SCHEMA = "org.frankensargo.duranium-import-journal/1"
ATTEST_SCHEMA = "org.frankensargo.duranium-lv-attestation/1"
LVM_UUID_RE = re.compile(r"[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}\Z")
SHA_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})\Z")
SERIAL_RE = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
SAFE_DEVICE_RE = re.compile(r"/dev/[A-Za-z0-9._+-]+\Z")
MAX_JSON = 8 * 1024 * 1024
MAX_VGCFG = 4 * 1024 * 1024
MAX_EXTENTS = 131072
MAXIMUM_REVIEWED_METADATA_PERCENT = decimal.Decimal("75.00")
REQUIRED_PENDING_TAGS = frozenset(
    {"distro.duranium", "greygoo.import-pending", "greygoo.replaceable"}
)
REQUIRED_PUBLISHED_TAGS = frozenset(
    {"distro.duranium", "greygoo.replaceable", "pocketboot.disk.v1"}
)
LVM_CONFIG = (
    "devices/use_devicesfile=0 activation/auto_activation_volume_list=[] "
    "activation/read_only_volume_list=[] backup/backup=0 backup/archive=0"
)
REPORT_CONFIG = "backup/backup=0 backup/archive=0"


class ImportFailure(RuntimeError):
    pass


def fail(message: str) -> "None":
    raise ImportFailure(message)


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def sha256_bytes(value: bytes, *, prefix: bool = False) -> str:
    digest = hashlib.sha256(value).hexdigest()
    return f"sha256:{digest}" if prefix else digest


def checked_sha(value: object, field: str, *, prefix: bool = False) -> str:
    if not isinstance(value, str):
        fail(f"{field} is not a SHA-256 string")
    match = SHA_RE.fullmatch(value)
    if match is None or (prefix and not value.startswith("sha256:")):
        fail(f"{field} is not a canonical SHA-256")
    return match.group(1)


def checked_lvm_uuid(value: object, field: str) -> str:
    if not isinstance(value, str) or not LVM_UUID_RE.fullmatch(value):
        fail(f"{field} is not a canonical LVM UUID")
    return value


def checked_uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        fail(f"{field} is not a UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        fail(f"{field} is not a UUID")
    if value != str(parsed):
        fail(f"{field} is not a canonical lowercase UUID")
    return value


def checked_int(value: object, field: str, *, positive: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        fail(f"{field} is not a decimal integer")
    text = str(value)
    if not text.isascii() or not text.isdecimal():
        fail(f"{field} is not a decimal integer")
    result = int(text)
    if positive and result <= 0:
        fail(f"{field} must be positive")
    return result


def required_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        fail(f"{field} is not an object")
    return dict(value)


def required_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        fail(f"{field} is not an array")
    return list(value)


def parse_json(data: bytes, field: str) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"{field} is not valid UTF-8 JSON: {error}")
    return required_dict(value, field)


@dataclasses.dataclass
class HeldFile:
    field: str
    path: Path
    fd: int
    identity: tuple[int, int, int, int, int]
    size: int
    sha256: str

    @classmethod
    def open(cls, field: str, path: Path, maximum: int | None = None) -> "HeldFile":
        flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except OSError as error:
            fail(f"cannot open {field} without following a final symlink: {path}: {error}")
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                fail(f"{field} is not a regular file: {path}")
            if maximum is not None and info.st_size > maximum:
                fail(f"{field} exceeds its {maximum}-byte limit")
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                fail(f"{field} is locked for modification: {path}")
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
            digest = hashlib.sha256()
            offset = 0
            while offset < info.st_size:
                block = os.pread(fd, min(1024 * 1024, info.st_size - offset), offset)
                if not block:
                    fail(f"{field} ended while hashing")
                digest.update(block)
                offset += len(block)
            item = cls(field, path.resolve(strict=True), fd, identity, info.st_size, digest.hexdigest())
            item.verify()
            return item
        except BaseException:
            os.close(fd)
            raise

    def read(self) -> bytes:
        output = bytearray()
        while len(output) < self.size:
            block = os.pread(self.fd, min(1024 * 1024, self.size - len(output)), len(output))
            if not block:
                fail(f"{self.field} ended while reading")
            output.extend(block)
        self.verify()
        return bytes(output)

    def hash_region(self, offset: int, count: int) -> str:
        if offset < 0 or count <= 0 or offset + count > self.size:
            fail(f"{self.field} extent lies outside the held file")
        digest = hashlib.sha256()
        position = offset
        remaining = count
        while remaining:
            block = os.pread(self.fd, min(1024 * 1024, remaining), position)
            if not block:
                fail(f"{self.field} ended within an extent")
            digest.update(block)
            position += len(block)
            remaining -= len(block)
        self.verify()
        return digest.hexdigest()

    def verify(self) -> None:
        info = os.fstat(self.fd)
        current = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if current != self.identity:
            fail(f"{self.field} changed while held: {self.path}")
        try:
            path_info = os.stat(self.path, follow_symlinks=False)
        except OSError as error:
            fail(f"{self.field} path disappeared while held: {error}")
        path_identity = (
            path_info.st_dev,
            path_info.st_ino,
            path_info.st_size,
            path_info.st_mtime_ns,
            path_info.st_ctime_ns,
        )
        if path_identity != self.identity or not stat.S_ISREG(path_info.st_mode):
            fail(f"{self.field} pathname no longer names the held regular file")

    def json_identity(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "device": self.identity[0],
            "inode": self.identity[1],
            "bytes": self.size,
            "mtime_ns": self.identity[3],
            "ctime_ns": self.identity[4],
            "sha256": self.sha256,
        }

    def close(self) -> None:
        os.close(self.fd)


@dataclasses.dataclass(frozen=True)
class Binding:
    serial: str
    partuuid: str
    anchor: str
    pv_uuid: str
    vg_uuid: str
    pool_uuid: str
    pool_data_uuid: str
    pool_metadata_uuid: str
    disk_uuid: str
    disk_bytes: int
    pool_bytes: int
    chunk_bytes: int
    minimum_free_bytes: int
    operation_uuid: str
    authorization_sha256: str
    plan_sha256: str
    checkpoint_sha256: str
    checkpoint_state_sha256: str
    all_lvm_uuids: tuple[str, ...]
    publish_argv: tuple[str, ...]

    @property
    def disk_sectors(self) -> int:
        return self.disk_bytes // 512

    @property
    def disk_dm_uuid(self) -> str:
        return "LVM-" + self.vg_uuid.replace("-", "") + self.disk_uuid.replace("-", "")


@dataclasses.dataclass(frozen=True)
class Extent:
    ordinal: int
    start_chunk: int
    chunk_count: int
    source_offset: int
    source_bytes: int
    destination_offset: int
    sha256: str

    def json(self) -> dict[str, object]:
        return {
            "ordinal": self.ordinal,
            "start_chunk": self.start_chunk,
            "chunk_count": self.chunk_count,
            "source_offset_bytes": self.source_offset,
            "source_bytes": self.source_bytes,
            "destination_offset_bytes": self.destination_offset,
            "sha256": self.sha256,
        }


@dataclasses.dataclass(frozen=True)
class AuditContract:
    value: dict[str, object]
    sha256: str
    disk_sha256: str
    provenance_sha256: str
    adapter_sha256: str
    full_lv_sha256: str
    mapped_upper_bytes: int
    maximum_metadata_percent: str
    extents: tuple[Extent, ...]


@dataclasses.dataclass(frozen=True)
class LiveState:
    serial: str
    partuuid: str
    anchor: str
    pv_uuid: str
    vg_uuid: str
    pool_uuid: str
    pool_data_uuid: str
    pool_metadata_uuid: str
    pool_bytes: int
    pool_chunk_bytes: int
    pool_segtype: str
    pool_data_percent: str
    pool_metadata_percent: str
    pool_healthy: bool
    pool_discards: str
    pool_when_full: str
    disk_uuid: str
    disk_bytes: int
    disk_segtype: str
    disk_pool_uuid: str
    disk_tags: frozenset[str]
    disk_permission: str
    disk_active: bool
    disk_dm_uuid: str | None
    disk_sectors: int | None
    disk_kernel_ro: bool | None
    disk_quiescent: bool

    def pool_usage(self) -> tuple[str, str]:
        return self.pool_data_percent, self.pool_metadata_percent

    def json(self) -> dict[str, object]:
        value = dataclasses.asdict(self)
        value["disk_tags"] = sorted(self.disk_tags)
        return value


@dataclasses.dataclass(frozen=True)
class RemoteResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes
    trustworthy_remote_status: bool


def activate_writable_argv(binding: Binding) -> tuple[str, ...]:
    return (
        "/sbin/lvm.static", "lvchange", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--activate", "y", "franken/disk-duranium",
    )


def sync_import_argv(binding: Binding) -> tuple[str, ...]:
    del binding
    return ("/bin/sync",)


def make_readonly_argv(binding: Binding) -> tuple[str, ...]:
    deactivate = [
        "/sbin/lvm.static", "lvchange", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--activate", "n", "franken/disk-duranium",
    ]
    activate = [
        "/sbin/lvm.static", "lvchange", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--activate", "y", "franken/disk-duranium",
    ]
    script = (
        "set -eu; " + shlex.join(deactivate) + "; " + shlex.join(activate)
        + "; /sbin/blockdev --setro /dev/mapper/franken-disk--duranium"
    )
    return ("/bin/sh", "-c", script)


def deactivate_argv(binding: Binding) -> tuple[str, ...]:
    deactivate = [
        "/sbin/lvm.static", "lvchange", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--activate", "n", "franken/disk-duranium",
    ]
    activate_pool = [
        "/sbin/lvm.static", "lvchange", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--activate", "y", "franken/pool",
    ]
    return (
        "/bin/sh", "-c",
        "set -eu; " + shlex.join(deactivate) + "; " + shlex.join(activate_pool),
    )


def publish_argv(binding: Binding) -> tuple[str, ...]:
    return binding.publish_argv


def attest_argv(binding: Binding, expected_sha256: str) -> tuple[str, ...]:
    script = (
        "set -eu; p=/dev/mapper/franken-disk--duranium; "
        "sha_applet=$(/bin/readlink -f /usr/bin/sha256sum); test \"$sha_applet\" = /bin/busybox; "
        "check(){ n=$(/bin/readlink -f \"$p\"); case $n in /dev/dm-[0-9]*) ;; *) exit 41;; esac; "
        "d=${n##*/}; test \"$(/bin/cat /sys/class/block/$d/dm/uuid)\" = "
        + shlex.quote(binding.disk_dm_uuid) + "; "
        "test \"$(/bin/cat /sys/class/block/$d/size)\" = " + str(binding.disk_sectors) + "; "
        "test \"$(/bin/cat /sys/class/block/$d/ro)\" = 1; }; "
        "check; actual=$(/usr/bin/sha256sum \"$n\"); actual=${actual%% *}; "
        "test \"$actual\" = " + shlex.quote(expected_sha256) + "; check; "
        "printf 'FRANKENSARGO_DURANIUM_SHA256_V1|%s\\n' \"$actual\""
    )
    return ("/bin/sh", "-c", script)


def capture_vgcfg_argv(binding: Binding) -> tuple[str, ...]:
    remote_path = f"/run/frankensargo-duranium-{binding.operation_uuid}.vgcfg"
    backup = [
        "/sbin/lvm.static", "vgcfgbackup", "--devices", binding.anchor, "--nohints",
        "--config", LVM_CONFIG, "--readonly", "--file", remote_path, "franken",
    ]
    script = (
        "set -eu; /bin/rm -f " + shlex.quote(remote_path) + "; "
        + shlex.join(backup) + " 1>&2; exec /bin/cat " + shlex.quote(remote_path)
    )
    return ("/bin/sh", "-c", script)


_EXTENT_WRITE_SCRIPT = """set -eu
op=$1 ordinal=$2 bytes=$3 expected=$4 chunk=$5 start=$6 count=$7 dm_uuid=$8 sectors=$9
p=/dev/mapper/franken-disk--duranium
n=$(/bin/readlink -f "$p")
case $n in /dev/dm-[0-9]*) ;; *) exit 41;; esac
d=${n##*/}
test "$(/bin/cat /sys/class/block/$d/dm/uuid)" = "$dm_uuid"
test "$(/bin/cat /sys/class/block/$d/size)" = "$sectors"
test "$(/bin/cat /sys/class/block/$d/ro)" = 0
test "$(/bin/readlink -f /usr/bin/sha256sum)" = /bin/busybox
tmp=/run/frankensargo-duranium-$op-$ordinal.input
readback=/run/frankensargo-duranium-$op-$ordinal.readback
cleanup(){ /bin/busybox rm -f "$tmp" "$readback"; }
trap cleanup EXIT HUP INT TERM
cleanup
umask 077
/bin/busybox dd of="$tmp" bs=1048576 2>/dev/null
test "$(/bin/busybox wc -c <"$tmp")" = "$bytes"
actual=$(/usr/bin/sha256sum "$tmp"); actual=${actual%% *}
test "$actual" = "$expected"
/bin/busybox dd if="$tmp" of="$n" bs="$chunk" seek="$start" conv=notrunc 2>/dev/null
/bin/sync
/sbin/blockdev --flushbufs "$n"
/bin/busybox rm -f "$tmp"
/bin/busybox dd if="$n" of="$readback" bs="$chunk" skip="$start" count="$count" 2>/dev/null
test "$(/bin/busybox wc -c <"$readback")" = "$((chunk * count))"
actual=$(/bin/busybox head -c "$bytes" "$readback" | /usr/bin/sha256sum); actual=${actual%% *}
test "$actual" = "$expected"
/sbin/blockdev --flushbufs "$n"
printf 'FRANKENSARGO_DURANIUM_EXTENT_V1|%s|%s|%s\n' "$ordinal" "$bytes" "$actual"
"""


_EXTENT_READBACK_SCRIPT = """set -eu
op=$1 ordinal=$2 bytes=$3 expected=$4 chunk=$5 start=$6 count=$7 dm_uuid=$8 sectors=$9
p=/dev/mapper/franken-disk--duranium
n=$(/bin/readlink -f "$p")
case $n in /dev/dm-[0-9]*) ;; *) exit 41;; esac
d=${n##*/}
test "$(/bin/cat /sys/class/block/$d/dm/uuid)" = "$dm_uuid"
test "$(/bin/cat /sys/class/block/$d/size)" = "$sectors"
test "$(/bin/readlink -f /usr/bin/sha256sum)" = /bin/busybox
readback=/run/frankensargo-duranium-$op-$ordinal.verify
cleanup(){ /bin/busybox rm -f "$readback"; }
trap cleanup EXIT HUP INT TERM
cleanup
umask 077
/bin/busybox dd if="$n" of="$readback" bs="$chunk" skip="$start" count="$count" 2>/dev/null
test "$(/bin/busybox wc -c <"$readback")" = "$((chunk * count))"
actual=$(/bin/busybox head -c "$bytes" "$readback" | /usr/bin/sha256sum); actual=${actual%% *}
test "$actual" = "$expected"
printf 'FRANKENSARGO_DURANIUM_EXTENT_READBACK_V1|%s|%s|%s\n' "$ordinal" "$bytes" "$actual"
"""


def _extent_script_argv(script: str, binding: Binding, extent: Extent) -> tuple[str, ...]:
    return (
        "/bin/sh", "-c", script, "frankensargo-duranium-extent-v1",
        binding.operation_uuid, str(extent.ordinal), str(extent.source_bytes), extent.sha256,
        str(binding.chunk_bytes), str(extent.start_chunk), str(extent.chunk_count),
        binding.disk_dm_uuid, str(binding.disk_sectors),
    )


def write_extent_argv(binding: Binding, extent: Extent) -> tuple[str, ...]:
    return _extent_script_argv(_EXTENT_WRITE_SCRIPT, binding, extent)


def verify_extent_argv(binding: Binding, extent: Extent) -> tuple[str, ...]:
    return _extent_script_argv(_EXTENT_READBACK_SCRIPT, binding, extent)


def extent_success_stdout(extent: Extent) -> bytes:
    return (
        f"FRANKENSARGO_DURANIUM_EXTENT_V1|{extent.ordinal}|"
        f"{extent.source_bytes}|{extent.sha256}\n"
    ).encode("ascii")


def extent_readback_stdout(extent: Extent) -> bytes:
    return (
        f"FRANKENSARGO_DURANIUM_EXTENT_READBACK_V1|{extent.ordinal}|"
        f"{extent.source_bytes}|{extent.sha256}\n"
    ).encode("ascii")


class Remote(Protocol):
    def require_trustworthy_status(self, serial: str) -> None: ...
    def observe(self, binding: Binding) -> LiveState: ...
    def activate_writable(self, binding: Binding) -> RemoteResult: ...
    def write_extent(self, binding: Binding, disk: HeldFile, extent: Extent) -> RemoteResult: ...
    def verify_extent(self, binding: Binding, extent: Extent) -> RemoteResult: ...
    def sync(self, binding: Binding) -> RemoteResult: ...
    def make_readonly(self, binding: Binding) -> RemoteResult: ...
    def attest(self, binding: Binding, expected_sha256: str) -> tuple[RemoteResult, dict[str, object]]: ...
    def deactivate(self, binding: Binding) -> RemoteResult: ...
    def publish(self, binding: Binding) -> RemoteResult: ...
    def capture_vgcfg(self, binding: Binding) -> tuple[RemoteResult, bytes]: ...


def _dir_fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_mkdir(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    if not cursor.is_dir() or cursor.is_symlink():
        fail(f"state parent is not a real directory: {cursor}")
    for item in reversed(missing):
        item.mkdir(mode=0o700)
        _dir_fsync(item.parent)
    if not path.is_dir() or path.is_symlink():
        fail(f"state path is not a real directory: {path}")


def atomic_write(path: Path, data: bytes) -> None:
    durable_mkdir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            amount = os.write(fd, view)
            if amount <= 0:
                fail(f"short host write for {path}")
            view = view[amount:]
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(temporary, path)
        _dir_fsync(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


class Journal:
    def __init__(self, root: Path, *, allow_volatile: bool = False):
        if not root.is_absolute() or root != root.resolve(strict=False):
            fail("state directory must be an absolute canonical path")
        if not allow_volatile and (root == Path("/tmp") or Path("/tmp") in root.parents):
            fail("state directory must not be below /tmp")
        durable_mkdir(root)
        self.root = root
        self.events = root / "events"
        self.extents = root / "extents"
        durable_mkdir(self.events)
        durable_mkdir(self.extents)
        lock_path = root / "controller.lock"
        if not lock_path.exists():
            atomic_write(lock_path, b"")
        self.lock_fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lock_fd)
            fail("another Duranium import controller holds the state lock")

    def close(self) -> None:
        os.close(self.lock_fd)

    def path(self, relative: str) -> Path:
        if relative.startswith("/") or ".." in Path(relative).parts:
            fail("invalid journal-relative path")
        return self.root / relative

    def write_once(self, relative: str, value: object) -> dict[str, object]:
        if not isinstance(value, dict):
            fail("journal record must be an object")
        path = self.path(relative)
        data = canonical_bytes(value)
        if path.exists():
            observed = self.read(relative)
            if canonical_bytes(observed) != data:
                fail(f"existing journal record differs: {relative}")
            return observed
        atomic_write(path, data)
        return dict(value)

    def read(self, relative: str) -> dict[str, object]:
        path = self.path(relative)
        held = HeldFile.open(f"journal {relative}", path, MAX_JSON)
        try:
            return parse_json(held.read(), f"journal {relative}")
        finally:
            held.close()

    def optional(self, relative: str) -> dict[str, object] | None:
        return self.read(relative) if self.path(relative).exists() else None


def _tags(value: object, field: str) -> frozenset[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        result = list(value)
    elif isinstance(value, str):
        result = [item for item in value.split(",") if item]
    else:
        fail(f"{field} is not a tag collection")
    if len(result) != len(set(result)):
        fail(f"{field} contains duplicate tags")
    return frozenset(result)


def _lv_name(value: object) -> str:
    if not isinstance(value, str):
        fail("checkpoint LV name is malformed")
    name = value.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if not re.fullmatch(r"[A-Za-z0-9+_.-]+", name):
        fail("checkpoint LV name is unsafe")
    return name


def _recompute_plan_authorization(plan: dict[str, object]) -> str:
    core = {key: value for key, value in plan.items() if key not in {"authorization_sha256", "confirmation"}}
    return "sha256:" + hashlib.sha256(canonical_bytes(core)).hexdigest()


def validate_plan_checkpoint(
    plan_file: HeldFile,
    checkpoint_file: HeldFile,
    serial: str,
    partuuid: str,
    audit: dict[str, object],
) -> Binding:
    plan = parse_json(plan_file.read(), "bootstrap plan")
    checkpoint = parse_json(checkpoint_file.read(), "final bootstrap checkpoint")
    if plan.get("schema") != PLAN_SCHEMA or plan.get("action") != "bootstrap-userdata-anchor":
        fail("bootstrap plan has the wrong schema or action")
    if _recompute_plan_authorization(plan) != plan.get("authorization_sha256"):
        fail("bootstrap plan authorization hash is invalid")
    authorization = str(plan["authorization_sha256"])
    operation_uuid = checked_uuid(plan.get("operation_uuid"), "plan operation UUID")
    device = required_dict(plan.get("device"), "plan.device")
    if (
        device.get("fastboot_serial") != serial
        or device.get("product") != "sargo"
        or device.get("compatible") != "google,sargo"
    ):
        fail("bootstrap plan device identity differs from the armed exact sargo serial")
    partition = required_dict(plan.get("partition"), "plan.partition")
    if checked_uuid(partition.get("partuuid"), "plan userdata PARTUUID") != partuuid:
        fail("plan userdata PARTUUID differs from the armed PARTUUID")
    if partition.get("kernel_name_observation") != "mmcblk0p72":
        fail("plan userdata kernel-node observation is not the fixed sargo partition")
    lvm = required_dict(plan.get("lvm"), "plan.lvm")
    if lvm.get("vg_name") != "franken":
        fail("plan VG name is not franken")
    pv_plan = required_dict(lvm.get("pv"), "plan.lvm.pv")
    planned_pv_uuid = checked_lvm_uuid(pv_plan.get("planned_uuid"), "planned PV UUID")
    volumes = required_list(lvm.get("volumes"), "plan.lvm.volumes")
    by_name: dict[str, dict[str, object]] = {}
    for index, raw in enumerate(volumes):
        volume = required_dict(raw, f"plan volume {index}")
        name = volume.get("name")
        if not isinstance(name, str) or name in by_name:
            fail("plan volumes have missing or duplicate names")
        by_name[name] = volume
    disk_plan = by_name.get("disk-duranium")
    pool_plan = by_name.get("pool")
    if disk_plan is None or pool_plan is None:
        fail("plan lacks the Duranium disk or thin pool")
    if (
        disk_plan.get("kind") != "thin"
        or _tags(disk_plan.get("tags"), "planned disk tags") != REQUIRED_PENDING_TAGS
        or required_dict(disk_plan.get("allocation"), "planned disk allocation").get("thin_pool") != "pool"
    ):
        fail("plan Duranium destination is not the exact pending thin LV")
    disk_bytes = checked_int(disk_plan.get("virtual_bytes"), "planned disk virtual bytes")
    pool_bytes = checked_int(pool_plan.get("size_bytes"), "planned pool bytes")
    if disk_bytes != 20 * 1024 * 1024 * 1024 or pool_bytes != 20 * 1024 * 1024 * 1024:
        fail("plan must retain the reviewed 20 GiB Duranium LV and 20 GiB thin pool")
    if (
        pool_plan.get("kind") != "thin-data"
        or _tags(pool_plan.get("tags"), "planned pool tags")
        != frozenset({"greygoo.replaceable", "greygoo.thin-pool.v1"})
    ):
        fail("plan thin-pool data LV role or tags differ from the reviewed layout")
    thin = required_dict(lvm.get("thin_pool"), "plan thin pool")
    chunk_bytes = checked_int(thin.get("chunk_bytes"), "planned pool chunk bytes")
    if (
        chunk_bytes != 256 * 1024
        or thin.get("name") != "pool"
        or thin.get("discard_policy") != "nopassdown"
        or thin.get("error_when_full") is not True
    ):
        fail("plan thin-pool safety policy is not exact")

    expected_checkpoint_keys = {
        "schema", "authorization_sha256", "operation_uuid", "ordinal", "stage", "step",
        "recovered_exact_postcondition", "lvm_state_sha256", "lvm_state", "generated_ids", "vgcfgbackup",
    }
    if (
        set(checkpoint) != expected_checkpoint_keys
        or checkpoint.get("schema") != CHECKPOINT_SCHEMA
        or checkpoint.get("authorization_sha256") != authorization
        or checkpoint.get("operation_uuid") != operation_uuid
        or checkpoint.get("ordinal") != 12
        or checkpoint.get("stage") != 11
        or checkpoint.get("step") != "backup-vg-metadata"
        or not isinstance(checkpoint.get("recovered_exact_postcondition"), bool)
    ):
        fail("final bootstrap checkpoint body or binding is invalid")
    lvm_state = required_dict(checkpoint.get("lvm_state"), "checkpoint lvm_state")
    if set(lvm_state) != {"pvs", "vgs", "lvs"}:
        fail("checkpoint LVM state is incomplete")
    checkpoint_state_sha = "sha256:" + hashlib.sha256(canonical_bytes(lvm_state)).hexdigest()
    if checkpoint.get("lvm_state_sha256") != checkpoint_state_sha:
        fail("checkpoint LVM-state hash is invalid")
    ids = required_dict(checkpoint.get("generated_ids"), "checkpoint generated_ids")
    if set(ids) != {"pv_uuid", "vg_uuid", "lv_uuids"}:
        fail("checkpoint generated UUID set is incomplete")
    pv_uuid = checked_lvm_uuid(ids.get("pv_uuid"), "checkpoint PV UUID")
    vg_uuid = checked_lvm_uuid(ids.get("vg_uuid"), "checkpoint VG UUID")
    if pv_uuid != planned_pv_uuid:
        fail("checkpoint PV UUID differs from the plan")
    lv_ids_raw = required_dict(ids.get("lv_uuids"), "checkpoint LV UUIDs")
    lv_ids = {name: checked_lvm_uuid(value, f"checkpoint {name} LV UUID") for name, value in lv_ids_raw.items()}
    for required in ("pool", "pool_tdata", "pool_tmeta", "disk-duranium"):
        if required not in lv_ids:
            fail(f"checkpoint lacks the {required} LV UUID")

    pvs = required_list(lvm_state.get("pvs"), "checkpoint pvs")
    vgs = required_list(lvm_state.get("vgs"), "checkpoint vgs")
    lvs = required_list(lvm_state.get("lvs"), "checkpoint lvs")
    if len(pvs) != 1 or len(vgs) != 1:
        fail("checkpoint does not contain exactly one PV and VG")
    pv_row = required_dict(pvs[0], "checkpoint PV row")
    vg_row = required_dict(vgs[0], "checkpoint VG row")
    anchor = str(pv_row.get("pv_name", "")).strip()
    if not SAFE_DEVICE_RE.fullmatch(anchor):
        fail("checkpoint PV path is unsafe")
    if pv_row.get("pv_uuid") != pv_uuid or pv_row.get("vg_uuid") != vg_uuid:
        fail("checkpoint PV row disagrees with generated UUIDs")
    if vg_row.get("vg_uuid") != vg_uuid or str(vg_row.get("vg_name", "")).strip() != "franken":
        fail("checkpoint VG row disagrees with generated UUIDs")
    rows: dict[str, dict[str, object]] = {}
    observed_ids: dict[str, str] = {}
    for index, raw in enumerate(lvs):
        row = required_dict(raw, f"checkpoint LV row {index}")
        name = _lv_name(row.get("lv_name"))
        if name in rows:
            fail(f"checkpoint has duplicate LV rows for {name}")
        if row.get("vg_uuid") != vg_uuid:
            fail(f"checkpoint {name} LV belongs to another VG")
        observed_ids[name] = checked_lvm_uuid(row.get("lv_uuid"), f"checkpoint {name} row UUID")
        rows[name] = row
    if observed_ids != lv_ids:
        fail("checkpoint generated LV UUIDs do not exactly match its complete LV report")
    capture = required_dict(checkpoint.get("vgcfgbackup"), "checkpoint vgcfgbackup")
    if set(capture) != {"bytes", "sha256", "generated_ids", "remote_source", "host_destination"}:
        fail("final bootstrap checkpoint vgcfgbackup evidence is incomplete")
    if capture.get("generated_ids") != ids:
        fail("final bootstrap vgcfgbackup UUID binding differs from the checkpoint")
    capture_bytes = checked_int(capture.get("bytes"), "final bootstrap vgcfgbackup bytes")
    capture_sha = checked_sha(capture.get("sha256"), "final bootstrap vgcfgbackup SHA-256", prefix=True)
    host_destination_raw = capture.get("host_destination")
    if not isinstance(host_destination_raw, str):
        fail("final bootstrap vgcfgbackup host path is malformed")
    host_destination = Path(host_destination_raw)
    if (
        checkpoint_file.path.parent.name != "steps"
        or not host_destination.is_absolute()
        or host_destination != host_destination.resolve(strict=False)
        or not host_destination.is_relative_to(checkpoint_file.path.parent.parent)
    ):
        fail("final bootstrap vgcfgbackup lies outside its durable bootstrap state directory")
    captured_file = HeldFile.open("final bootstrap vgcfgbackup", host_destination, MAX_VGCFG)
    try:
        captured_data = captured_file.read()
        if len(captured_data) != capture_bytes or captured_file.sha256 != capture_sha:
            fail("final bootstrap vgcfgbackup file differs from its checkpoint evidence")
        try:
            captured_text = captured_data.decode("ascii")
        except UnicodeDecodeError:
            fail("final bootstrap vgcfgbackup is not ASCII")
        captured_ids = re.findall(r'(?m)^\s*id\s*=\s*"([^"]+)"\s*$', captured_text)
        if len(captured_ids) != len(set(captured_ids)) or set(captured_ids) != {pv_uuid, vg_uuid, *lv_ids.values()}:
            fail("final bootstrap vgcfgbackup UUID set differs from its checkpoint LVM state")
        if not re.search(r"(?m)^franken\s*\{\s*$", captured_text):
            fail("final bootstrap vgcfgbackup lacks the franken VG stanza")
    finally:
        captured_file.close()
    disk_row = rows["disk-duranium"]
    pool_row = rows["pool"]
    if (
        str(disk_row.get("segtype", "")).strip() != "thin"
        or checked_int(disk_row.get("lv_size"), "checkpoint disk size") != disk_bytes
        or _tags(disk_row.get("lv_tags"), "checkpoint disk tags") != REQUIRED_PENDING_TAGS
        or disk_row.get("pool_lv_uuid") != lv_ids["pool"]
        or "pocketboot.disk.v1" in _tags(disk_row.get("lv_tags"), "checkpoint disk tags")
    ):
        fail("checkpoint disk LV is not the exact unpublished destination")
    disk_attr = str(disk_row.get("lv_attr", "")).strip()
    if len(disk_attr) < 2 or disk_attr[0] != "V" or disk_attr[1] != "w":
        fail("checkpoint disk LV is not writable thin metadata")
    if (
        str(pool_row.get("segtype", "")).strip() != "thin-pool"
        or checked_int(pool_row.get("lv_size"), "checkpoint pool size") != pool_bytes
        or pool_row.get("data_lv_uuid") != lv_ids["pool_tdata"]
        or pool_row.get("metadata_lv_uuid") != lv_ids["pool_tmeta"]
        or str(pool_row.get("discards", "")).strip() != "nopassdown"
        or str(pool_row.get("lv_when_full", "")).strip() != "error"
    ):
        fail("checkpoint thin-pool identity or policy is invalid")

    audit_destination = required_dict(audit.get("destination"), "audit destination")
    audit_gate = required_dict(audit.get("thin_pool_gate"), "audit thin-pool gate")
    if checked_int(audit_destination.get("virtual_bytes"), "audit virtual bytes") != disk_bytes:
        fail("audit destination size differs from the planned LV")
    if checked_int(audit_gate.get("pool_bytes"), "audit pool bytes") != pool_bytes:
        fail("audit pool size differs from the planned pool")
    if checked_int(audit_gate.get("chunk_bytes"), "audit chunk bytes") != chunk_bytes:
        fail("audit chunk size differs from the planned pool")
    minimum_free = checked_int(audit_gate.get("required_minimum_pool_free_bytes"), "audit minimum pool free")
    if minimum_free != 16 * 1024 * 1024 * 1024:
        fail("audit does not retain the mandatory 16 GiB pool headroom")
    maximum_metadata_raw = audit_gate.get("maximum_pool_metadata_percent")
    if not isinstance(maximum_metadata_raw, str):
        fail("audit lacks an explicit maximum pool metadata percentage")
    maximum_metadata = _percentage(
        maximum_metadata_raw, "audit maximum pool metadata percent"
    )
    if maximum_metadata_raw != f"{maximum_metadata:.2f}":
        fail("audit maximum pool metadata percent is not canonical to two decimals")
    if maximum_metadata <= 0 or maximum_metadata > MAXIMUM_REVIEWED_METADATA_PERCENT:
        fail("audit maximum pool metadata percentage exceeds the reviewed 75.00 bound")

    transaction = required_dict(plan.get("transaction"), "plan.transaction")
    post = required_list(transaction.get("post_import_argv"), "plan post_import_argv")
    if len(post) != 1:
        fail("plan must contain exactly one post-import publication command")
    publication = required_dict(post[0], "plan publication command")
    argv = required_list(publication.get("argv"), "plan publication argv")
    if not all(isinstance(item, str) for item in argv):
        fail("plan publication argv contains a non-string")
    expected = [
        "/sbin/lvm.static", "lvchange", "--devices", "@USERDATA_BLOCK_DEVICE@", "--nohints",
        "--config", REPORT_CONFIG, "--permission", "r", "--deltag", "greygoo.import-pending",
        "--addtag", "pocketboot.disk.v1", "franken/disk-duranium",
    ]
    if argv != expected or publication.get("step") != "publish-verified-duranium-disk":
        fail("plan post-import mutation is not the exact reviewed publication argv")
    resolved_publish = tuple(anchor if item == "@USERDATA_BLOCK_DEVICE@" else item for item in argv)
    return Binding(
        serial=serial, partuuid=partuuid, anchor=anchor, pv_uuid=pv_uuid, vg_uuid=vg_uuid,
        pool_uuid=lv_ids["pool"], pool_data_uuid=lv_ids["pool_tdata"],
        pool_metadata_uuid=lv_ids["pool_tmeta"], disk_uuid=lv_ids["disk-duranium"],
        disk_bytes=disk_bytes, pool_bytes=pool_bytes, chunk_bytes=chunk_bytes,
        minimum_free_bytes=minimum_free, operation_uuid=operation_uuid,
        authorization_sha256=authorization, plan_sha256=plan_file.sha256,
        checkpoint_sha256=checkpoint_file.sha256, checkpoint_state_sha256=checkpoint_state_sha,
        all_lvm_uuids=tuple(sorted({pv_uuid, vg_uuid, *lv_ids.values()})),
        publish_argv=resolved_publish,
    )


def parse_audit(value: dict[str, object], raw_sha: str) -> AuditContract:
    if value.get("schema") != AUDIT_SCHEMA:
        fail("Duranium audit has the wrong schema")
    inputs = required_dict(value.get("inputs"), "audit.inputs")
    disk = required_dict(inputs.get("disk"), "audit disk")
    provenance = required_dict(inputs.get("provenance"), "audit provenance")
    adapter = required_dict(inputs.get("adapter"), "audit adapter")
    destination = required_dict(value.get("destination"), "audit destination")
    gate = required_dict(value.get("thin_pool_gate"), "audit thin-pool gate")
    contract = required_dict(value.get("import_contract"), "audit import contract")
    chunk = checked_int(contract.get("write_block_bytes"), "audit write block bytes")
    raw_extents = required_list(contract.get("write_extents"), "audit extents")
    if len(raw_extents) > MAX_EXTENTS:
        fail("audit has too many write extents")
    extents: list[Extent] = []
    previous_end = 0
    for ordinal, raw in enumerate(raw_extents):
        item = required_dict(raw, f"audit extent {ordinal}")
        start = checked_int(item.get("start_chunk"), f"extent {ordinal} start", positive=False)
        count = checked_int(item.get("chunk_count"), f"extent {ordinal} chunks")
        source_offset = checked_int(item.get("source_offset_bytes"), f"extent {ordinal} source offset", positive=False)
        source_bytes = checked_int(item.get("source_bytes"), f"extent {ordinal} source bytes")
        destination_offset = checked_int(item.get("destination_offset_bytes"), f"extent {ordinal} destination offset", positive=False)
        digest = checked_sha(item.get("sha256"), f"extent {ordinal} SHA-256")
        if (
            source_offset != start * chunk
            or destination_offset != source_offset
            or source_bytes > count * chunk
            or source_bytes <= (count - 1) * chunk
        ):
            fail(f"audit extent {ordinal} geometry is inconsistent")
        if ordinal and start < previous_end:
            fail("audit extents overlap or are reordered")
        previous_end = start + count
        extents.append(Extent(ordinal, start, count, source_offset, source_bytes, destination_offset, digest))
    maximum_metadata_raw = gate.get("maximum_pool_metadata_percent")
    if not isinstance(maximum_metadata_raw, str):
        fail("audit maximum pool metadata percent is not a string")
    maximum_metadata = _percentage(
        maximum_metadata_raw, "audit maximum pool metadata percent"
    )
    if maximum_metadata_raw != f"{maximum_metadata:.2f}":
        fail("audit maximum pool metadata percent is not canonical to two decimals")
    if maximum_metadata <= 0 or maximum_metadata > MAXIMUM_REVIEWED_METADATA_PERCENT:
        fail("audit maximum pool metadata percent exceeds the reviewed 75.00 bound")
    mapped_upper = checked_int(
        gate.get("new_mapped_bytes_upper_bound"),
        "audit mapped upper bound",
        positive=False,
    )
    exact_extent_upper = sum(extent.chunk_count * chunk for extent in extents)
    if mapped_upper != exact_extent_upper:
        fail("audit mapped upper bound differs from the exact sparse extent ceiling")
    return AuditContract(
        value=value, sha256=raw_sha,
        disk_sha256=checked_sha(disk.get("sha256"), "audit disk SHA-256"),
        provenance_sha256=checked_sha(provenance.get("sha256"), "audit provenance SHA-256"),
        adapter_sha256=checked_sha(adapter.get("sha256"), "audit adapter SHA-256"),
        full_lv_sha256=checked_sha(destination.get("full_lv_sha256"), "audit full-LV SHA-256"),
        mapped_upper_bytes=mapped_upper,
        maximum_metadata_percent=f"{maximum_metadata:.2f}",
        extents=tuple(extents),
    )


def validate_pocketboot_runtime(
    image: HeldFile,
    provenance: HeldFile,
    binding: Binding,
    patch_directory: Path,
) -> dict[str, object]:
    value = parse_json(provenance.read(), "bound PocketBoot provenance")
    if value.get("format") != POCKETBOOT_PROVENANCE_SCHEMA or value.get("profile") != "interim-lvm-bound-lab":
        fail("PocketBoot provenance has the wrong bound-image format/profile")
    output = required_dict(value.get("output"), "PocketBoot provenance output")
    if (
        output.get("basename") != image.path.name
        or checked_int(output.get("bytes"), "PocketBoot image bytes") != image.size
        or checked_sha(output.get("sha256"), "PocketBoot image SHA-256") != image.sha256
    ):
        fail("PocketBoot image bytes/hash do not match its bound provenance")
    runtime_binding = required_dict(value.get("binding"), "PocketBoot provenance binding")
    if runtime_binding.get("vg_uuid") != binding.vg_uuid or runtime_binding.get("pv_partuuids") != [binding.partuuid]:
        fail("PocketBoot image is not bound to the final bootstrap VG/userdata PV")
    cmdline = runtime_binding.get("kernel_cmdline")
    if not isinstance(cmdline, str):
        fail("PocketBoot provenance kernel command line is malformed")
    tokens = cmdline.split()
    required_tokens = (
        f"pocketboot.vg_uuid={binding.vg_uuid}",
        f"pocketboot.pv_partuuid={binding.partuuid}",
        "sysrq_always_enabled=1",
    )
    for token in required_tokens:
        if tokens.count(token) != 1:
            fail(f"PocketBoot bound kernel command line lacks exactly one {token}")
    if sum(token.startswith("pocketboot.vg_uuid=") for token in tokens) != 1 or sum(
        token.startswith("pocketboot.pv_partuuid=") for token in tokens
    ) != 1:
        fail("PocketBoot bound kernel command line contains an extra storage binding")

    recorded_raw = required_list(value.get("patches"), "PocketBoot provenance patches")
    recorded: dict[str, str] = {}
    for index, raw in enumerate(recorded_raw):
        item = required_dict(raw, f"PocketBoot patch {index}")
        if set(item) != {"name", "sha256"} or not isinstance(item.get("name"), str):
            fail("PocketBoot provenance contains a malformed patch record")
        name = str(item["name"])
        if name in recorded or not re.fullmatch(r"[0-9]{4}-[A-Za-z0-9._+-]+\.patch", name):
            fail("PocketBoot provenance contains a duplicate or unsafe patch name")
        recorded[name] = checked_sha(item.get("sha256"), f"PocketBoot patch {name} SHA-256")
    current: dict[str, str] = {}
    for path in sorted(patch_directory.glob("*.patch")):
        patch = HeldFile.open(f"current PocketBoot patch {path.name}", path, MAX_JSON)
        try:
            current[path.name] = patch.sha256
        finally:
            patch.close()
    if recorded != current:
        fail("PocketBoot provenance patch set differs from this exact frankensargo checkout")
    for required in (
        "0009-read-only-ums.patch",
        "0010-enable-sha256sum.patch",
        "0011-safe-gadget-teardown-recovery.patch",
        "0013-adb-shell-v2-status.patch",
    ):
        if required not in recorded:
            fail(f"PocketBoot image lacks required safety patch {required}")
    image.verify()
    provenance.verify()
    return {
        "image": image.json_identity(),
        "provenance": provenance.json_identity(),
        "patches": [{"name": name, "sha256": recorded[name]} for name in sorted(recorded)],
        "kernel_cmdline": cmdline,
    }


AuditRunner = Callable[[Sequence[str], HeldFile], dict[str, object]]


def run_audit_subprocess(argv: Sequence[str], tool: HeldFile) -> dict[str, object]:
    tool.verify()
    command = [sys.executable, f"/proc/self/fd/{tool.fd}", *argv]
    process = subprocess.run(
        command,
        pass_fds=(tool.fd,),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    tool.verify()
    if process.returncode != 0:
        detail = process.stderr.decode("utf-8", "replace").strip()
        fail(f"independent audit-duranium-import failed ({process.returncode}): {detail}")
    if len(process.stdout) > MAX_JSON:
        fail("independent Duranium audit output is oversized")
    return parse_json(process.stdout, "independent Duranium audit output")


def independently_reaudit(
    provided: dict[str, object],
    binding: Binding,
    disk: HeldFile,
    provenance: HeldFile,
    adapter: HeldFile,
    audit_tool: HeldFile,
    runner: AuditRunner = run_audit_subprocess,
) -> AuditContract:
    preliminary = parse_audit(provided, sha256_bytes(canonical_bytes(provided)))
    gate = required_dict(provided.get("thin_pool_gate"), "audit thin-pool gate")
    import_contract = required_dict(provided.get("import_contract"), "audit import contract")
    destination = required_dict(provided.get("destination"), "audit destination")
    if (
        import_contract.get("skip_all_zero_blocks") is not True
        or import_contract.get("preserve_unwritten_zero_tail") is not True
        or checked_int(import_contract.get("readback_bytes"), "audit readback bytes") != binding.disk_bytes
        or checked_sha(import_contract.get("readback_sha256"), "audit readback SHA-256")
        != preliminary.full_lv_sha256
        or checked_int(destination.get("zero_tail_bytes"), "audit zero-tail bytes", positive=False)
        != binding.disk_bytes - disk.size
        or gate.get("requires_live_pre_and_post_pool_usage_check") is not True
        or gate.get("requires_live_before_every_remaining_extent_check") is not True
        or gate.get("requires_live_full_hash_and_publication_check") is not True
        or checked_int(gate.get("sparse_write_extent_count"), "audit sparse extent count", positive=False)
        != len(preliminary.extents)
    ):
        fail("audit sparse-import/readback policy is incomplete")
    maximum_write = checked_int(import_contract.get("maximum_write_bytes"), "audit maximum write bytes")
    if maximum_write > 64 * 1024 * 1024 or maximum_write % binding.chunk_bytes:
        fail("audit maximum write size exceeds the reviewed 64 MiB aligned bound")
    if any(extent.source_bytes > maximum_write for extent in preliminary.extents):
        fail("audit contains an oversized sparse-write extent")
    argv = [
        "--disk", str(disk.path), "--provenance", str(provenance.path),
        "--adapter", str(adapter.path), "--userdata-partuuid", binding.partuuid,
        "--pv-uuid", binding.pv_uuid, "--vg-uuid", binding.vg_uuid,
        "--disk-lv-uuid", binding.disk_uuid, "--virtual-bytes", str(binding.disk_bytes),
        "--pool-bytes", str(binding.pool_bytes), "--chunk-bytes", str(binding.chunk_bytes),
        "--max-write-bytes", str(maximum_write), "--minimum-pool-free-bytes",
        str(binding.minimum_free_bytes), "--maximum-pool-metadata-percent",
        preliminary.maximum_metadata_percent,
    ]
    rerun = runner(argv, audit_tool)
    if canonical_bytes(rerun) != canonical_bytes(provided):
        fail("provided Duranium audit differs from a fresh independent audit")
    contract = parse_audit(rerun, sha256_bytes(canonical_bytes(rerun)))
    binding_value = required_dict(rerun.get("binding"), "audit binding")
    expected_binding = {
        "userdata_partuuid": binding.partuuid,
        "pv_uuid": binding.pv_uuid,
        "vg_uuid": binding.vg_uuid,
        "disk_lv_name": "disk-duranium",
        "disk_lv_uuid": binding.disk_uuid,
        "required_import_tag": "greygoo.import-pending",
        "published_tag": "pocketboot.disk.v1",
    }
    if binding_value != expected_binding:
        fail("Duranium audit binding differs from the final bootstrap checkpoint")
    if contract.disk_sha256 != disk.sha256:
        fail("held derived disk SHA-256 differs from the fresh audit")
    if contract.provenance_sha256 != provenance.sha256:
        fail("held provenance SHA-256 differs from the fresh audit")
    if contract.adapter_sha256 != adapter.sha256:
        fail("held adapter SHA-256 differs from the fresh audit")
    for extent in contract.extents:
        if disk.hash_region(extent.source_offset, extent.source_bytes) != extent.sha256:
            fail(f"fresh audit extent {extent.ordinal} does not match the held disk")
    disk.verify()
    provenance.verify()
    adapter.verify()
    return contract


def _result_json(result: RemoteResult, transaction: str, intent_sha: str) -> dict[str, object]:
    return {
        "schema": JOURNAL_SCHEMA,
        "completion": "typed-shell-v2-result",
        "transaction_sha256": transaction,
        "intent_sha256": intent_sha,
        "argv": list(result.argv),
        "returncode": result.returncode,
        "trustworthy_remote_status": result.trustworthy_remote_status,
        "stdout_bytes": len(result.stdout),
        "stdout_sha256": sha256_bytes(result.stdout),
        "stderr_bytes": len(result.stderr),
        "stderr_sha256": sha256_bytes(result.stderr),
    }


def _require_result(
    value: dict[str, object],
    *,
    transaction: str,
    intent_sha: str,
    argv: Sequence[str],
    stdout: bytes | None = None,
    stderr: bytes | None = None,
) -> None:
    expected_keys = {
        "schema", "completion", "transaction_sha256", "intent_sha256", "argv", "returncode",
        "trustworthy_remote_status", "stdout_bytes", "stdout_sha256", "stderr_bytes", "stderr_sha256",
    }
    if (
        set(value) != expected_keys
        or value.get("schema") != JOURNAL_SCHEMA
        or value.get("completion") != "typed-shell-v2-result"
        or value.get("transaction_sha256") != transaction
        or value.get("intent_sha256") != intent_sha
        or value.get("returncode") != 0
        or value.get("trustworthy_remote_status") is not True
    ):
        fail("remote result record does not prove a successful trustworthy remote exit")
    if value.get("argv") != list(argv):
        fail("remote result argv differs from its exact intent")
    for field in ("stdout_bytes", "stderr_bytes"):
        checked_int(value.get(field), f"remote result {field}", positive=False)
    checked_sha(value.get("stdout_sha256"), "remote stdout SHA-256")
    checked_sha(value.get("stderr_sha256"), "remote stderr SHA-256")
    if stdout is not None and (
        value.get("stdout_bytes") != len(stdout)
        or value.get("stdout_sha256") != sha256_bytes(stdout)
    ):
        fail("remote result stdout evidence differs from the exact expected output")
    if stderr is not None and (
        value.get("stderr_bytes") != len(stderr)
        or value.get("stderr_sha256") != sha256_bytes(stderr)
    ):
        fail("remote result stderr evidence differs from the exact expected output")


def _require_remote_success(result: RemoteResult, field: str) -> None:
    if not result.trustworthy_remote_status:
        fail(f"{field} lacks a trustworthy remote exit status")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        fail(f"{field} failed remotely with status {result.returncode}: {detail}")


def _require_exact_remote_success(
    result: RemoteResult,
    field: str,
    argv: Sequence[str],
    *,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
) -> None:
    _require_remote_success(result, field)
    if result.argv != tuple(argv):
        fail(f"{field} returned status for argv other than the exact regenerated command")
    if stdout is not None and result.stdout != stdout:
        fail(f"{field} returned stdout other than the exact expected evidence")
    if stderr is not None and result.stderr != stderr:
        fail(f"{field} returned stderr other than the exact expected evidence")


def _percentage(value: str, field: str) -> decimal.Decimal:
    try:
        parsed = decimal.Decimal(value)
    except decimal.InvalidOperation:
        fail(f"{field} is not a decimal percentage")
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        fail(f"{field} lies outside 0..100")
    if parsed.as_tuple().exponent < -6:
        fail(f"{field} has unbounded report precision")
    return parsed


def _usage_upper(pool_bytes: int, value: str, field: str) -> int:
    percentage = _percentage(value, field)
    quantum = decimal.Decimal(1).scaleb(percentage.as_tuple().exponent)
    conservative = min(decimal.Decimal(100), percentage + quantum)
    bytes_value = decimal.Decimal(pool_bytes) * conservative / decimal.Decimal(100)
    return int(bytes_value.to_integral_value(rounding=decimal.ROUND_CEILING))


def validate_live_identity(state: LiveState, binding: Binding) -> None:
    expected = (
        (state.serial, binding.serial, "serial"),
        (state.partuuid, binding.partuuid, "userdata PARTUUID"),
        (state.anchor, binding.anchor, "userdata node"),
        (state.pv_uuid, binding.pv_uuid, "PV UUID"),
        (state.vg_uuid, binding.vg_uuid, "VG UUID"),
        (state.pool_uuid, binding.pool_uuid, "pool UUID"),
        (state.pool_data_uuid, binding.pool_data_uuid, "pool data UUID"),
        (state.pool_metadata_uuid, binding.pool_metadata_uuid, "pool metadata UUID"),
        (state.disk_uuid, binding.disk_uuid, "Duranium LV UUID"),
        (state.disk_pool_uuid, binding.pool_uuid, "Duranium pool link"),
        (state.disk_bytes, binding.disk_bytes, "Duranium LV size"),
        (state.disk_segtype, "thin", "Duranium LV segment type"),
        (state.pool_bytes, binding.pool_bytes, "pool size"),
        (state.pool_chunk_bytes, binding.chunk_bytes, "pool chunk size"),
        (state.pool_segtype, "thin-pool", "pool segment type"),
        (state.pool_discards, "nopassdown", "pool discard policy"),
        (state.pool_when_full, "error", "pool when-full policy"),
    )
    for actual, wanted, field in expected:
        if actual != wanted:
            fail(f"live {field} differs from the transaction binding")
    if not state.pool_healthy:
        fail("live thin pool is not healthy")
    if not state.disk_quiescent:
        fail("live Duranium LV is mounted, swapped, held, or exposed through UMS")
    for value, field in (
        (state.pool_data_percent, "live pool data percent"),
        (state.pool_metadata_percent, "live pool metadata percent"),
    ):
        if value:
            _percentage(value, field)
        elif state.disk_active:
            fail(f"{field} is unavailable while the import target is active")


def validate_pending(state: LiveState, binding: Binding, *, active: bool | None, readonly: bool | None) -> None:
    validate_live_identity(state, binding)
    if state.disk_tags != REQUIRED_PENDING_TAGS or state.disk_permission != "rw":
        fail("live Duranium LV is not the exact writable import-pending object")
    if active is not None and state.disk_active is not active:
        fail("live Duranium LV activation differs from the expected transaction phase")
    if not state.disk_active:
        if any(item is not None for item in (state.disk_dm_uuid, state.disk_sectors, state.disk_kernel_ro)):
            fail("inactive Duranium LV unexpectedly reports device-mapper state")
    else:
        if (
            state.disk_dm_uuid != binding.disk_dm_uuid
            or state.disk_sectors != binding.disk_sectors
            or (readonly is not None and state.disk_kernel_ro is not readonly)
        ):
            fail("active Duranium mapping identity/size/read-only state is wrong")


def validate_published(state: LiveState, binding: Binding) -> None:
    validate_live_identity(state, binding)
    if (
        state.disk_tags != REQUIRED_PUBLISHED_TAGS
        or state.disk_permission != "r"
        or state.disk_active
        or state.disk_dm_uuid is not None
        or state.disk_sectors is not None
        or state.disk_kernel_ro is not None
    ):
        fail("live Duranium LV is not the exact inactive read-only published object")


PHASES = (
    "00-activate-writable",
    "01-sync-import",
    "02-post-write-state",
    "03-make-readonly",
    "04-readonly-state",
    "05-full-lv-attestation",
    "06-post-attestation-state",
    "07-deactivate",
    "08-pre-publication-state",
    "09-publish",
    "10-published-state",
    "11-vgcfgbackup",
    "12-complete",
)


class ImportController:
    def __init__(
        self,
        *,
        binding: Binding,
        contract: AuditContract,
        disk: HeldFile,
        provenance: HeldFile,
        adapter: HeldFile,
        pocketboot_image: HeldFile,
        pocketboot_provenance: HeldFile,
        pocketboot_runtime: dict[str, object],
        remote: Remote,
        journal: Journal,
        audit_tool_sha256: str,
        implementation_hashes: dict[str, str],
        implementation_files: Sequence[HeldFile] = (),
    ):
        self.binding = binding
        self.contract = contract
        self.disk = disk
        self.provenance = provenance
        self.adapter = adapter
        self.pocketboot_image = pocketboot_image
        self.pocketboot_provenance = pocketboot_provenance
        self.remote = remote
        self.journal = journal
        expected_implementations = {"entrypoint", "controller", "shell_v2", "audit_tool"}
        if set(implementation_hashes) != expected_implementations:
            fail("import implementation hash set is incomplete")
        for name, digest in implementation_hashes.items():
            checked_sha(digest, f"{name} implementation SHA-256")
        if implementation_hashes["audit_tool"] != audit_tool_sha256:
            fail("audit-tool implementation hash differs from its held input")
        self.implementation_hashes = dict(sorted(implementation_hashes.items()))
        self.implementation_files = tuple(implementation_files)
        intent = {
            "schema": SCHEMA,
            "serial": binding.serial,
            "userdata_partuuid": binding.partuuid,
            "operation_uuid": binding.operation_uuid,
            "bootstrap_authorization_sha256": binding.authorization_sha256,
            "bootstrap_plan_sha256": binding.plan_sha256,
            "bootstrap_checkpoint_sha256": binding.checkpoint_sha256,
            "bootstrap_lvm_state_sha256": binding.checkpoint_state_sha256,
            "audit_sha256": contract.sha256,
            "audit_tool_sha256": audit_tool_sha256,
            "implementation_sha256": self.implementation_hashes,
            "safety_policy": {
                "required_minimum_pool_free_bytes": binding.minimum_free_bytes,
                "maximum_pool_metadata_percent": contract.maximum_metadata_percent,
                "extent_completion": "target-flush-and-exact-readback-v1",
                "resume_completed_extent": "live-exact-readback-required-v1",
            },
            "source_files": {
                "disk": disk.json_identity(),
                "provenance": provenance.json_identity(),
                "adapter": adapter.json_identity(),
                "pocketboot_image": pocketboot_image.json_identity(),
                "pocketboot_provenance": pocketboot_provenance.json_identity(),
            },
            "pocketboot_runtime": pocketboot_runtime,
            "destination": {
                "anchor": binding.anchor,
                "pv_uuid": binding.pv_uuid,
                "vg_uuid": binding.vg_uuid,
                "pool_uuid": binding.pool_uuid,
                "pool_data_uuid": binding.pool_data_uuid,
                "pool_metadata_uuid": binding.pool_metadata_uuid,
                "disk_uuid": binding.disk_uuid,
                "disk_bytes": binding.disk_bytes,
                "full_lv_sha256": contract.full_lv_sha256,
            },
        }
        self.intent = self.journal.write_once("intent.json", intent)
        self.transaction = sha256_bytes(canonical_bytes(self.intent))
        self._validate_journal_names()

    def _validate_journal_names(self) -> None:
        allowed_root = {
            "controller.lock", "intent.json", "preflight-state.json", "pre-write-state.json", "events", "extents",
            "franken-post-import.vgcfg", "audit.json",
        }
        unexpected_root = {item.name for item in self.journal.root.iterdir()} - allowed_root
        if unexpected_root:
            fail(f"state directory contains unexpected entries: {sorted(unexpected_root)}")
        allowed_events = {
            f"{phase}.{suffix}.json" for phase in PHASES for suffix in ("intent", "result", "state")
        }
        unexpected_events = {item.name for item in self.journal.events.iterdir()} - allowed_events
        if unexpected_events:
            fail(f"event journal contains unexpected entries: {sorted(unexpected_events)}")
        extent_re = re.compile(r"[0-9]{8}\.(?:intent|result)\.json\Z")
        unexpected_extents = [item.name for item in self.journal.extents.iterdir() if not extent_re.fullmatch(item.name)]
        if unexpected_extents:
            fail(f"extent journal contains unexpected entries: {sorted(unexpected_extents)}")

    def _verify_sources(self) -> None:
        for item, expected in (
            (self.disk, self.contract.disk_sha256),
            (self.provenance, self.contract.provenance_sha256),
            (self.adapter, self.contract.adapter_sha256),
        ):
            item.verify()
            if item.sha256 != expected:
                fail(f"held {item.field} hash differs from the fresh audit")
        self.pocketboot_image.verify()
        self.pocketboot_provenance.verify()
        for item in self.implementation_files:
            item.verify()
        observed = {item.field: item.sha256 for item in self.implementation_files}
        for name, digest in self.implementation_hashes.items():
            # Production HeldFile fields use these stable implementation names.
            if name in observed and observed[name] != digest:
                fail(f"held {name} implementation hash changed")

    def _phase_intent(self, phase: str, payload: dict[str, object]) -> tuple[dict[str, object], str]:
        if phase not in PHASES:
            fail("unknown import phase")
        index = PHASES.index(phase)
        for earlier in PHASES[:index]:
            if not any(self.journal.path(f"events/{earlier}.{suffix}.json").exists() for suffix in ("result", "state")):
                fail(f"journal skips required earlier phase {earlier}")
        value = {
            "schema": JOURNAL_SCHEMA,
            "transaction_sha256": self.transaction,
            "phase": phase,
            **payload,
        }
        own_path = self.journal.path(f"events/{phase}.intent.json")
        if not own_path.exists():
            for later in PHASES[index + 1 :]:
                if any(self.journal.path(f"events/{later}.{suffix}.json").exists() for suffix in ("intent", "result", "state")):
                    fail(f"journal contains future phase {later} before {phase}")
        stored = self.journal.write_once(f"events/{phase}.intent.json", value)
        return stored, sha256_bytes(canonical_bytes(stored))

    def _complete_remote_phase(
        self,
        phase: str,
        payload: dict[str, object],
        expected_argv: Sequence[str],
        invoke: Callable[[], RemoteResult],
        *,
        recover_if: Callable[[LiveState], bool] | None = None,
        expected_stdout: bytes | None = None,
        expected_stderr: bytes | None = None,
    ) -> dict[str, object]:
        if "argv" in payload and payload.get("argv") != list(expected_argv):
            fail(f"{phase} payload argv differs from the regenerated exact command")
        intent, intent_sha = self._phase_intent(
            phase, {**payload, "argv": list(expected_argv)}
        )
        relative = f"events/{phase}.result.json"
        existing = self.journal.optional(relative)
        if existing is not None:
            if existing.get("completion") == "recovered-exact-postcondition":
                expected_keys = {
                    "schema", "completion", "transaction_sha256", "intent_sha256",
                    "argv", "postcondition", "postcondition_sha256",
                }
                postcondition = required_dict(
                    existing.get("postcondition"), f"{phase} recovered postcondition"
                )
                if (
                    set(existing) != expected_keys
                    or existing.get("schema") != JOURNAL_SCHEMA
                    or existing.get("transaction_sha256") != self.transaction
                    or existing.get("intent_sha256") != intent_sha
                    or existing.get("argv") != list(expected_argv)
                    or existing.get("postcondition_sha256")
                    != sha256_bytes(canonical_bytes(postcondition))
                    or recover_if is None
                ):
                    fail(f"{phase} recovered result is corrupt or lacks an exact command binding")
                observed = self.remote.observe(self.binding)
                try:
                    recovered = recover_if(observed)
                except ImportFailure:
                    recovered = False
                if not recovered:
                    fail(f"{phase} recovered result no longer has its exact live postcondition")
            else:
                _require_result(
                    existing,
                    transaction=self.transaction,
                    intent_sha=intent_sha,
                    argv=expected_argv,
                    stdout=expected_stdout,
                    stderr=expected_stderr,
                )
            return existing
        if recover_if is not None:
            observed = self.remote.observe(self.binding)
            try:
                recovered = recover_if(observed)
            except ImportFailure:
                recovered = False
            if recovered:
                postcondition = observed.json()
                return self.journal.write_once(
                    relative,
                    {
                        "schema": JOURNAL_SCHEMA,
                        "completion": "recovered-exact-postcondition",
                        "transaction_sha256": self.transaction,
                        "intent_sha256": intent_sha,
                        "argv": list(expected_argv),
                        "postcondition": postcondition,
                        "postcondition_sha256": sha256_bytes(canonical_bytes(postcondition)),
                    },
                )
        result = invoke()
        value = _result_json(result, self.transaction, intent_sha)
        if not result.trustworthy_remote_status:
            fail(f"{phase} lacks a trustworthy remote exit status")
        if result.argv != tuple(expected_argv):
            fail(f"{phase} returned status for argv other than the exact regenerated command")
        stored = self.journal.write_once(relative, value)
        _require_result(
            stored,
            transaction=self.transaction,
            intent_sha=intent_sha,
            argv=expected_argv,
            stdout=expected_stdout,
            stderr=expected_stderr,
        )
        return stored

    def _record_state(self, phase: str, state: LiveState) -> dict[str, object]:
        intent, intent_sha = self._phase_intent(phase, {"kind": "live-state"})
        value = {
            "schema": JOURNAL_SCHEMA,
            "transaction_sha256": self.transaction,
            "intent_sha256": intent_sha,
            "state": state.json(),
        }
        return self.journal.write_once(f"events/{phase}.state.json", value)

    def _record_root_state(self, name: str, state: LiveState) -> dict[str, object]:
        if name not in {"preflight-state", "pre-write-state"}:
            fail("invalid root live-state checkpoint name")
        return self.journal.write_once(
            f"{name}.json",
            {
                "schema": JOURNAL_SCHEMA,
                "transaction_sha256": self.transaction,
                "state": state.json(),
            },
        )

    def _read_root_state(self, name: str) -> LiveState:
        value = self.journal.read(f"{name}.json")
        if (
            set(value) != {"schema", "transaction_sha256", "state"}
            or value.get("schema") != JOURNAL_SCHEMA
            or value.get("transaction_sha256") != self.transaction
        ):
            fail(f"{name} checkpoint is corrupt or stale")
        raw = required_dict(value.get("state"), name)
        raw["disk_tags"] = frozenset(required_list(raw.get("disk_tags"), f"{name} disk tags"))
        try:
            return LiveState(**raw)  # type: ignore[arg-type]
        except TypeError as error:
            fail(f"{name} checkpoint is incomplete: {error}")

    def _read_recorded_state(self, phase: str) -> LiveState:
        value = self.journal.read(f"events/{phase}.state.json")
        expected_keys = {"schema", "transaction_sha256", "intent_sha256", "state"}
        intent = self.journal.read(f"events/{phase}.intent.json")
        if (
            set(value) != expected_keys
            or value.get("schema") != JOURNAL_SCHEMA
            or value.get("transaction_sha256") != self.transaction
            or value.get("intent_sha256") != sha256_bytes(canonical_bytes(intent))
        ):
            fail(f"recorded state for {phase} has an invalid binding")
        raw = required_dict(value.get("state"), f"recorded {phase} state")
        try:
            raw["disk_tags"] = frozenset(required_list(raw.get("disk_tags"), "recorded disk tags"))
            return LiveState(**raw)  # type: ignore[arg-type]
        except TypeError as error:
            fail(f"recorded state for {phase} is incomplete: {error}")

    def _validate_extent_journal(self) -> None:
        names = {item.name for item in self.journal.extents.iterdir()}
        previous = self.transaction
        incomplete_seen = False
        for extent in self.contract.extents:
            prefix = f"{extent.ordinal:08d}"
            intent_name = f"{prefix}.intent.json"
            result_name = f"{prefix}.result.json"
            has_intent = intent_name in names
            has_result = result_name in names
            if has_result and not has_intent:
                fail(f"extent {extent.ordinal} result exists without intent")
            if incomplete_seen and (has_intent or has_result):
                fail("extent journal skips or reorders an extent")
            if not has_intent:
                incomplete_seen = True
                continue
            expected_intent = {
                "schema": JOURNAL_SCHEMA,
                "transaction_sha256": self.transaction,
                "kind": "sparse-write-extent",
                "previous_record_sha256": previous,
                "extent": extent.json(),
                "argv": list(write_extent_argv(self.binding, extent)),
            }
            observed_intent = self.journal.read(f"extents/{intent_name}")
            if observed_intent != expected_intent:
                fail(f"extent {extent.ordinal} intent is corrupt or stale")
            intent_sha = sha256_bytes(canonical_bytes(observed_intent))
            if not has_result:
                incomplete_seen = True
                previous = intent_sha
                continue
            observed_result = self.journal.read(f"extents/{result_name}")
            expected_result_keys = {
                "schema", "completion", "transaction_sha256", "intent_sha256", "argv", "returncode",
                "trustworthy_remote_status", "stdout_bytes", "stdout_sha256", "stderr_bytes",
                "stderr_sha256", "source_bytes", "source_sha256", "target_bytes",
                "target_readback_sha256", "durability_barrier",
            }
            if set(observed_result) != expected_result_keys:
                fail(f"extent {extent.ordinal} result is incomplete")
            _require_result(
                {
                    key: observed_result[key]
                    for key in observed_result
                    if key not in {
                        "source_bytes", "source_sha256", "target_bytes",
                        "target_readback_sha256", "durability_barrier",
                    }
                },
                transaction=self.transaction,
                intent_sha=intent_sha,
                argv=write_extent_argv(self.binding, extent),
                stdout=extent_success_stdout(extent),
                stderr=b"",
            )
            if observed_result.get("source_bytes") != extent.source_bytes or observed_result.get("source_sha256") != extent.sha256:
                fail(f"extent {extent.ordinal} result does not prove the exact source extent")
            if (
                observed_result.get("target_bytes") != extent.source_bytes
                or observed_result.get("target_readback_sha256") != extent.sha256
                or observed_result.get("durability_barrier") != "sync+blockdev-flushbufs+readback-v1"
            ):
                fail(f"extent {extent.ordinal} result lacks exact durable target-readback evidence")
            previous = sha256_bytes(canonical_bytes(observed_result))
        planned_names = {
            f"{extent.ordinal:08d}.{suffix}.json" for extent in self.contract.extents for suffix in ("intent", "result")
        }
        if names - planned_names:
            fail("extent journal contains an unplanned extent record")

    def _write_extents(self) -> None:
        self._validate_extent_journal()
        previous = self.transaction
        extent_uppers = [
            extent.chunk_count * self.binding.chunk_bytes for extent in self.contract.extents
        ]
        for index, extent in enumerate(self.contract.extents):
            prefix = f"{extent.ordinal:08d}"
            intent_relative = f"extents/{prefix}.intent.json"
            result_relative = f"extents/{prefix}.result.json"
            if self.journal.path(result_relative).exists():
                verify_argv = verify_extent_argv(self.binding, extent)
                verify = self.remote.verify_extent(self.binding, extent)
                _require_exact_remote_success(
                    verify,
                    f"extent {extent.ordinal} resume readback",
                    verify_argv,
                    stdout=extent_readback_stdout(extent),
                    stderr=b"",
                )
                previous = sha256_bytes(canonical_bytes(self.journal.read(result_relative)))
                continue
            actual = self.disk.hash_region(extent.source_offset, extent.source_bytes)
            if actual != extent.sha256:
                fail(f"held disk bytes changed for extent {extent.ordinal}")
            expected_intent = {
                "schema": JOURNAL_SCHEMA,
                "transaction_sha256": self.transaction,
                "kind": "sparse-write-extent",
                "previous_record_sha256": previous,
                "extent": extent.json(),
                "argv": list(write_extent_argv(self.binding, extent)),
            }
            intent = self.journal.write_once(intent_relative, expected_intent)
            intent_sha = sha256_bytes(canonical_bytes(intent))
            # Hash and durably journal the exact intent first, then take the
            # freshest possible live capacity/identity observation directly
            # before opening the shell-v2 input stream.
            current = self.remote.observe(self.binding)
            validate_pending(current, self.binding, active=True, readonly=False)
            remaining_upper = sum(extent_uppers[index:])
            self._validate_live_pool_gate(
                current,
                remaining_upper,
                f"before extent {extent.ordinal}",
            )
            result = self.remote.write_extent(self.binding, self.disk, extent)
            expected_argv = write_extent_argv(self.binding, extent)
            if not result.trustworthy_remote_status:
                fail(f"extent {extent.ordinal} lacks a trustworthy remote exit status")
            if result.argv != expected_argv:
                fail(
                    f"extent {extent.ordinal} returned status for argv other than "
                    "the exact regenerated command"
                )
            if result.returncode != 0:
                self.journal.write_once(
                    result_relative,
                    _result_json(result, self.transaction, intent_sha),
                )
                _require_remote_success(result, f"extent {extent.ordinal}")
            _require_exact_remote_success(
                result,
                f"extent {extent.ordinal}",
                expected_argv,
                stdout=extent_success_stdout(extent),
                stderr=b"",
            )
            record = {
                **_result_json(result, self.transaction, intent_sha),
                "source_bytes": extent.source_bytes,
                "source_sha256": actual,
                "target_bytes": extent.source_bytes,
                "target_readback_sha256": extent.sha256,
                "durability_barrier": "sync+blockdev-flushbufs+readback-v1",
            }
            self.journal.write_once(result_relative, record)
            previous = sha256_bytes(canonical_bytes(record))
        self._validate_extent_journal()

    def _validate_live_pool_gate(
        self, state: LiveState, remaining_allocation_upper: int, field: str
    ) -> None:
        validate_live_identity(state, self.binding)
        if not state.pool_data_percent or not state.pool_metadata_percent:
            fail(f"{field} lacks current live thin-pool data/metadata usage")
        used_upper = _usage_upper(
            state.pool_bytes, state.pool_data_percent, f"{field} pool data percent"
        )
        if (
            used_upper + remaining_allocation_upper
            > state.pool_bytes - self.binding.minimum_free_bytes
        ):
            fail(
                f"{field} current pool usage plus remaining audited allocation "
                "violates the mandatory 16 GiB headroom"
            )
        metadata = _percentage(state.pool_metadata_percent, f"{field} pool metadata percent")
        quantum = decimal.Decimal(1).scaleb(metadata.as_tuple().exponent)
        conservative_metadata = min(decimal.Decimal(100), metadata + quantum)
        maximum = decimal.Decimal(self.contract.maximum_metadata_percent)
        if conservative_metadata > maximum:
            fail(
                f"{field} conservative thin-pool metadata usage "
                f"{conservative_metadata}% exceeds the transaction limit {maximum}%"
            )

    def _attestation_expected(self) -> dict[str, object]:
        return {
            "schema": ATTEST_SCHEMA,
            "serial": self.binding.serial,
            "userdata_partuuid": self.binding.partuuid,
            "vg_uuid": self.binding.vg_uuid,
            "lv_uuid": self.binding.disk_uuid,
            "lvm_dm_uuid": self.binding.disk_dm_uuid,
            "sectors": self.binding.disk_sectors,
            "bytes": self.binding.disk_bytes,
            "ro": True,
            "sha_applet": "/bin/busybox",
            "expected_sha256": self.contract.full_lv_sha256,
            "actual_sha256": self.contract.full_lv_sha256,
        }

    def _validate_attestation_record(self) -> dict[str, object]:
        phase = "05-full-lv-attestation"
        intent = self.journal.read(f"events/{phase}.intent.json")
        result = self.journal.read(f"events/{phase}.result.json")
        expected_keys = {
            "schema", "transaction_sha256", "intent_sha256", "remote_result", "attestation",
        }
        if (
            set(result) != expected_keys
            or result.get("schema") != JOURNAL_SCHEMA
            or result.get("transaction_sha256") != self.transaction
            or result.get("intent_sha256") != sha256_bytes(canonical_bytes(intent))
        ):
            fail("full-LV attestation journal binding is invalid")
        remote_result = required_dict(result.get("remote_result"), "attestation remote result")
        _require_result(
            remote_result,
            transaction=self.transaction,
            intent_sha=sha256_bytes(canonical_bytes(intent)),
            argv=attest_argv(self.binding, self.contract.full_lv_sha256),
            stdout=(
                f"FRANKENSARGO_DURANIUM_SHA256_V1|{self.contract.full_lv_sha256}\n"
            ).encode("ascii"),
            stderr=b"",
        )
        attestation = required_dict(result.get("attestation"), "full-LV attestation")
        if attestation != self._attestation_expected():
            fail("full target-side 20 GiB LV attestation does not exactly match the audited destination")
        return result

    def execute(self) -> dict[str, object]:
        self._verify_sources()
        self.remote.require_trustworthy_status(self.binding.serial)
        # Validate the complete durable extent chain before any live mutation.
        # This also makes a typed nonzero result terminal before activation and
        # makes the remaining-allocation calculation below safe to derive from
        # result-file presence.
        self._validate_extent_journal()
        initial = self.remote.observe(self.binding)
        if initial.disk_tags == REQUIRED_PUBLISHED_TAGS:
            # Publication without this transaction's durable hash gate is never adopted.
            if any(
                not self.journal.path(path).exists()
                for path in (
                    "events/05-full-lv-attestation.result.json",
                    "events/06-post-attestation-state.state.json",
                    "events/08-pre-publication-state.state.json",
                    "events/09-publish.intent.json",
                )
            ):
                fail("Duranium LV is already published without this transaction's durable attestation and publication intent")
            self._validate_attestation_record()
            validate_published(initial, self.binding)
        else:
            validate_pending(initial, self.binding, active=None, readonly=None)
            if initial.pool_data_percent and initial.pool_metadata_percent:
                # An already-active pool can be capacity-gated before even the
                # destination activation metadata mutation.  If the inactive
                # pool omits usage, activation is followed immediately by the
                # mandatory current-state gate below.
                self._validate_live_pool_gate(
                    initial,
                    sum(
                        extent.chunk_count * self.binding.chunk_bytes
                        for extent in self.contract.extents
                        if not self.journal.path(
                            f"extents/{extent.ordinal:08d}.result.json"
                        ).exists()
                    ),
                    "initial live import gate",
                )

        pre_state_path = self.journal.path("preflight-state.json")
        if not pre_state_path.exists():
            validate_pending(initial, self.binding, active=False, readonly=None)
            self._record_root_state("preflight-state", initial)
        pre = self._read_root_state("preflight-state")
        validate_pending(pre, self.binding, active=False, readonly=None)
        if any(
            self.journal.path(path).exists()
            for path in (
                "events/02-post-write-state.state.json",
                "events/05-full-lv-attestation.result.json",
                "events/09-publish.intent.json",
            )
        ) and not self.journal.path("pre-write-state.json").exists():
            fail("advanced import journal lacks its active pre-write pool checkpoint")

        attestation_exists = self.journal.path("events/05-full-lv-attestation.result.json").exists()
        if initial.disk_tags != REQUIRED_PUBLISHED_TAGS and not attestation_exists:
            if not self.journal.path("events/04-readonly-state.state.json").exists():
                if not self.journal.path("events/02-post-write-state.state.json").exists():
                    self._complete_remote_phase(
                        "00-activate-writable", {"kind": "activate-exact-pending-lv"},
                        activate_writable_argv(self.binding),
                        lambda: self.remote.activate_writable(self.binding),
                        recover_if=lambda state: (
                            validate_pending(state, self.binding, active=True, readonly=False) is None
                        ),
                    )
                    writable = self.remote.observe(self.binding)
                    validate_pending(writable, self.binding, active=True, readonly=False)
                    self._validate_live_pool_gate(
                        writable, self.contract.mapped_upper_bytes, "after activation"
                    )
                    if not self.journal.path("pre-write-state.json").exists():
                        self._record_root_state("pre-write-state", writable)
                    pre_write = self._read_root_state("pre-write-state")
                    validate_pending(pre_write, self.binding, active=True, readonly=False)
                    self._write_extents()
                    self._complete_remote_phase(
                        "01-sync-import", {"kind": "flush-import"},
                        sync_import_argv(self.binding),
                        lambda: self.remote.sync(self.binding),
                        expected_stdout=b"", expected_stderr=b"",
                    )
                    post_write = self.remote.observe(self.binding)
                    validate_pending(post_write, self.binding, active=True, readonly=False)
                    self._validate_live_pool_gate(post_write, 0, "after sparse import")
                    self._record_state("02-post-write-state", post_write)
                post_write = self._read_recorded_state("02-post-write-state")
                pre_write = self._read_root_state("pre-write-state")
                validate_pending(pre_write, self.binding, active=True, readonly=False)
                validate_pending(post_write, self.binding, active=True, readonly=False)
                current_writable = self.remote.observe(self.binding)
                # The exact postcondition may already be present after a target
                # success/host-disconnect window.  The phase recovery below
                # distinguishes writable-pre from readonly-post state.
                validate_pending(current_writable, self.binding, active=True, readonly=None)
                self._validate_live_pool_gate(
                    current_writable, 0, "before read-only transition"
                )
                self._complete_remote_phase(
                    "03-make-readonly", {"kind": "deactivate-reactivate-and-setro"},
                    make_readonly_argv(self.binding),
                    lambda: self.remote.make_readonly(self.binding),
                    recover_if=lambda state: (
                        validate_pending(state, self.binding, active=True, readonly=True) is None
                    ),
                )
                readonly = self.remote.observe(self.binding)
                validate_pending(readonly, self.binding, active=True, readonly=True)
                self._validate_live_pool_gate(readonly, 0, "after read-only transition")
                self._record_state("04-readonly-state", readonly)
            readonly = self._read_recorded_state("04-readonly-state")
            validate_pending(readonly, self.binding, active=True, readonly=True)
            current_readonly = self.remote.observe(self.binding)
            validate_pending(current_readonly, self.binding, active=True, readonly=True)
            self._validate_live_pool_gate(current_readonly, 0, "before full-LV attestation")
            phase = "05-full-lv-attestation"
            expected_attest_argv = attest_argv(self.binding, self.contract.full_lv_sha256)
            intent, intent_sha = self._phase_intent(
                phase,
                {
                    "kind": "target-side-full-lv-sha256",
                    "expected": self._attestation_expected(),
                    "argv": list(expected_attest_argv),
                },
            )
            attestation_path = f"events/{phase}.result.json"
            if not self.journal.path(attestation_path).exists():
                remote_result, attestation = self.remote.attest(self.binding, self.contract.full_lv_sha256)
                if not remote_result.trustworthy_remote_status:
                    fail(f"{phase} lacks a trustworthy remote exit status")
                if remote_result.argv != expected_attest_argv:
                    fail(f"{phase} returned status for argv other than the exact regenerated command")
                self.journal.write_once(
                    attestation_path,
                    {
                        "schema": JOURNAL_SCHEMA,
                        "transaction_sha256": self.transaction,
                        "intent_sha256": intent_sha,
                        "remote_result": _result_json(remote_result, self.transaction, intent_sha),
                        "attestation": attestation,
                    },
                )
            self._validate_attestation_record()
            after_hash = self.remote.observe(self.binding)
            validate_pending(after_hash, self.binding, active=True, readonly=True)
            self._validate_live_pool_gate(after_hash, 0, "after full-LV attestation")
            self._record_state("06-post-attestation-state", after_hash)
            attestation_exists = True

        if attestation_exists:
            self._validate_attestation_record()
            if not self.journal.path("events/06-post-attestation-state.state.json").exists():
                readonly = self._read_recorded_state("04-readonly-state")
                after_hash = self.remote.observe(self.binding)
                validate_pending(after_hash, self.binding, active=True, readonly=True)
                self._validate_live_pool_gate(
                    after_hash, 0, "attestation checkpoint recovery"
                )
                self._record_state("06-post-attestation-state", after_hash)
        if initial.disk_tags != REQUIRED_PUBLISHED_TAGS:
            after_hash = self._read_recorded_state("06-post-attestation-state")
            current_after_hash = self.remote.observe(self.binding)
            # A completed deactivation with a lost host result is a valid
            # recoverable postcondition; publication is still separately gated.
            validate_pending(current_after_hash, self.binding, active=None, readonly=None)
            self._validate_live_pool_gate(
                current_after_hash, 0, "before verified-LV deactivation"
            )
            self._complete_remote_phase(
                "07-deactivate", {"kind": "deactivate-verified-lv"},
                deactivate_argv(self.binding),
                lambda: self.remote.deactivate(self.binding),
                recover_if=lambda state: (
                    validate_pending(state, self.binding, active=False, readonly=None) is None
                ),
            )
            before_publish = self.remote.observe(self.binding)
            validate_pending(before_publish, self.binding, active=False, readonly=None)
            self._validate_live_pool_gate(before_publish, 0, "before publication")
            self._record_state("08-pre-publication-state", before_publish)
            self._validate_attestation_record()
            self._complete_remote_phase(
                "09-publish", {"kind": "publish-exact-verified-lv", "argv": list(self.binding.publish_argv)},
                publish_argv(self.binding),
                lambda: self.remote.publish(self.binding),
                recover_if=lambda state: (validate_published(state, self.binding) is None),
            )
        else:
            self._complete_remote_phase(
                "09-publish", {"kind": "publish-exact-verified-lv", "argv": list(self.binding.publish_argv)},
                publish_argv(self.binding),
                lambda: self.remote.publish(self.binding),
                recover_if=lambda state: (validate_published(state, self.binding) is None),
            )

        published = self.remote.observe(self.binding)
        validate_published(published, self.binding)
        self._validate_live_pool_gate(published, 0, "after publication")
        self._record_state("10-published-state", published)
        self._validate_attestation_record()

        phase = "11-vgcfgbackup"
        expected_vgcfg_argv = capture_vgcfg_argv(self.binding)
        intent, intent_sha = self._phase_intent(
            phase,
            {
                "kind": "capture-post-publication-vgcfgbackup",
                "argv": list(expected_vgcfg_argv),
            },
        )
        result_path = f"events/{phase}.result.json"
        if not self.journal.path(result_path).exists():
            remote_result, data = self.remote.capture_vgcfg(self.binding)
            if not remote_result.trustworthy_remote_status:
                fail(f"{phase} lacks a trustworthy remote exit status")
            if remote_result.argv != expected_vgcfg_argv:
                fail(f"{phase} returned status for argv other than the exact regenerated command")
            if remote_result.returncode != 0:
                self.journal.write_once(
                    result_path,
                    {
                        "schema": JOURNAL_SCHEMA,
                        "transaction_sha256": self.transaction,
                        "intent_sha256": intent_sha,
                        "remote_result": _result_json(remote_result, self.transaction, intent_sha),
                        "vgcfgbackup": {"capture_failed": True},
                    },
                )
                _require_remote_success(remote_result, phase)
            evidence = validate_vgcfg(data, self.binding)
            vgcfg_path = self.journal.root / "franken-post-import.vgcfg"
            if vgcfg_path.exists():
                held = HeldFile.open("post-import vgcfgbackup", vgcfg_path, MAX_VGCFG)
                try:
                    if held.read() != data:
                        fail("existing durable post-import vgcfgbackup differs")
                finally:
                    held.close()
            else:
                atomic_write(vgcfg_path, data)
            self.journal.write_once(
                result_path,
                {
                    "schema": JOURNAL_SCHEMA,
                    "transaction_sha256": self.transaction,
                    "intent_sha256": intent_sha,
                    "remote_result": _result_json(remote_result, self.transaction, intent_sha),
                    "vgcfgbackup": evidence,
                },
            )
        validate_vgcfg_record(self.journal, self.transaction, self.binding)
        final_state = self.remote.observe(self.binding)
        validate_published(final_state, self.binding)
        self._validate_live_pool_gate(final_state, 0, "after vgcfgbackup")
        complete = {
            "schema": SCHEMA,
            "transaction_sha256": self.transaction,
            "serial": self.binding.serial,
            "userdata_partuuid": self.binding.partuuid,
            "disk_lv_uuid": self.binding.disk_uuid,
            "full_lv_sha256": self.contract.full_lv_sha256,
            "published": True,
            "vgcfgbackup_sha256": validate_vgcfg_record(self.journal, self.transaction, self.binding)["sha256"],
        }
        self._phase_intent("12-complete", {"kind": "terminal-checkpoint"})
        self.journal.write_once("events/12-complete.state.json", complete)
        self._verify_sources()
        return complete


def validate_vgcfg(data: bytes, binding: Binding) -> dict[str, object]:
    if not data or len(data) > MAX_VGCFG:
        fail("post-import vgcfgbackup is empty or oversized")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        fail("post-import vgcfgbackup is not ASCII")
    captured = re.findall(r'(?m)^\s*id\s*=\s*"([^"]+)"\s*$', text)
    if len(captured) != len(set(captured)):
        fail("post-import vgcfgbackup contains duplicate UUID assignments")
    for identifier in captured:
        checked_lvm_uuid(identifier, "vgcfgbackup UUID")
    if set(captured) != set(binding.all_lvm_uuids):
        fail("post-import vgcfgbackup UUID set differs from the bound complete LVM state")
    if not re.search(r"(?m)^franken\s*\{\s*$", text):
        fail("post-import vgcfgbackup lacks the franken VG stanza")
    return {"bytes": len(data), "sha256": sha256_bytes(data), "lvm_uuids": list(binding.all_lvm_uuids)}


def validate_vgcfg_record(journal: Journal, transaction: str, binding: Binding) -> dict[str, object]:
    phase = "11-vgcfgbackup"
    intent = journal.read(f"events/{phase}.intent.json")
    record = journal.read(f"events/{phase}.result.json")
    expected_keys = {"schema", "transaction_sha256", "intent_sha256", "remote_result", "vgcfgbackup"}
    if (
        set(record) != expected_keys
        or record.get("schema") != JOURNAL_SCHEMA
        or record.get("transaction_sha256") != transaction
        or record.get("intent_sha256") != sha256_bytes(canonical_bytes(intent))
    ):
        fail("vgcfgbackup result journal binding is invalid")
    remote_result = required_dict(record.get("remote_result"), "vgcfgbackup remote result")
    expected_argv = capture_vgcfg_argv(binding)
    _require_result(
        remote_result,
        transaction=transaction,
        intent_sha=sha256_bytes(canonical_bytes(intent)),
        argv=expected_argv,
    )
    held = HeldFile.open("durable post-import vgcfgbackup", journal.root / "franken-post-import.vgcfg", MAX_VGCFG)
    try:
        data = held.read()
        observed = validate_vgcfg(data, binding)
    finally:
        held.close()
    _require_result(
        remote_result,
        transaction=transaction,
        intent_sha=sha256_bytes(canonical_bytes(intent)),
        argv=expected_argv,
        stdout=data,
    )
    if record.get("vgcfgbackup") != observed:
        fail("durable post-import vgcfgbackup differs from its journal evidence")
    return observed


class AdbShellV2Remote:
    """Production remote API with an explicit shell_v2 protocol gate."""

    def __init__(self, adb: str, serial: str):
        self.serial = serial
        try:
            self.shell = AdbShellV2(adb, serial)
        except ShellV2Error as error:
            fail(f"could not initialize exact-serial ADB shell_v2 transport: {error}")
        self.qualified = False

    def require_trustworthy_status(self, serial: str) -> None:
        if serial != self.serial:
            fail("remote API serial differs from the transaction serial")
        try:
            self.shell.verify(timeout=30)
        except ShellV2Error as error:
            fail(
                "PocketBoot did not prove an exact-serial ADB shell_v2 command/status channel; "
                f"legacy or ambiguous ADB is refused: {error}"
            )
        self.qualified = True

    def run(
        self,
        argv: Sequence[str],
        *,
        input_file: BinaryIO | None = None,
        timeout: int | None = 120,
    ) -> RemoteResult:
        if not self.qualified:
            fail("ADB remote commands are forbidden before the shell_v2 protocol gate")
        if not argv or not all(isinstance(item, str) and "\0" not in item for item in argv):
            fail("remote argv is empty or malformed")
        try:
            result = self.shell.run(argv, stdin=input_file, timeout=timeout)
        except ShellV2Error as error:
            fail(f"ADB shell_v2 command did not produce a complete attested status: {error}")
        if result.argv != tuple(argv):
            fail("ADB shell_v2 returned status/evidence for argv other than the requested exact command")
        return RemoteResult(result.argv, result.returncode, result.stdout, result.stderr, True)

    def _report(self, applet: str, fields: str, target: str) -> list[dict[str, object]]:
        argv = [
            "/sbin/lvm.static", applet, "--devices", target if applet == "pvs" else "@ANCHOR@",
            "--nohints", "--config", REPORT_CONFIG, "--readonly", "--nolocking",
            "--reportformat", "json_std", "--units", "b", "--nosuffix",
        ]
        # Every applet must be fenced by the anchor, while lvs takes the VG as
        # its positional target.
        anchor_index = argv.index("@ANCHOR@") if "@ANCHOR@" in argv else None
        if anchor_index is not None:
            argv[anchor_index] = self._current_anchor
        if applet == "lvs":
            argv.extend(["-a", "-o", fields, target])
        else:
            argv.extend(["-o", fields, target])
        result = self.run(argv)
        _require_remote_success(result, f"fenced {applet} report")
        report = parse_json(result.stdout, f"fenced {applet} report")
        outer = required_list(report.get("report"), f"{applet} report array")
        if len(outer) != 1:
            fail(f"fenced {applet} report has multiple report objects")
        section = required_dict(outer[0], f"{applet} report object")
        singular = {"pvs": "pv", "vgs": "vg", "lvs": "lv"}[applet]
        rows = required_list(section.get(singular), f"{applet} rows")
        return [required_dict(row, f"{applet} row") for row in rows]

    def _resolve_anchor(self, binding: Binding) -> str:
        script = (
            "set -eu; found=; count=0; "
            "for u in /sys/class/block/*/uevent; do "
            f"if /bin/busybox grep -qx 'PARTUUID={binding.partuuid}' \"$u\"; then "
            "n=${u%/uevent}; n=${n##*/}; found=$n; count=$((count+1)); fi; done; "
            "test \"$count\" -eq 1; "
            "/bin/busybox grep -qx 'PARTNAME=userdata' \"/sys/class/block/$found/uevent\"; "
            "test \"$(/bin/cat /sys/class/block/$found/removable)\" = 0; "
            "printf '%s\\n' \"/dev/$found\""
        )
        result = self.run(["/bin/sh", "-c", script])
        _require_remote_success(result, "exact userdata PARTUUID resolution")
        try:
            anchor = result.stdout.decode("ascii").strip()
        except UnicodeDecodeError:
            fail("userdata resolver returned non-ASCII output")
        if anchor != binding.anchor:
            fail("live userdata PARTUUID resolves to a different node than the bootstrap checkpoint")
        return anchor

    def observe(self, binding: Binding) -> LiveState:
        self._current_anchor = self._resolve_anchor(binding)
        pvs = self._report("pvs", "pv_uuid,pv_name,vg_uuid,vg_name", self._current_anchor)
        lvs = self._report(
            "lvs",
            (
                "vg_uuid,lv_uuid,lv_name,lv_size,lv_active,lv_permissions,lv_tags,lv_attr,segtype,"
                "pool_lv_uuid,data_lv_uuid,metadata_lv_uuid,data_percent,metadata_percent,discards,"
                "lv_when_full,lv_health_status,chunk_size"
            ),
            "franken",
        )
        if len(pvs) != 1:
            fail("live fenced report does not contain exactly one userdata PV")
        rows: dict[str, dict[str, object]] = {}
        for row in lvs:
            name = _lv_name(row.get("lv_name"))
            if name in rows:
                fail(f"live LVM report has duplicate rows for {name}")
            rows[name] = row
        for name in ("pool", "pool_tdata", "pool_tmeta", "disk-duranium"):
            if name not in rows:
                fail(f"live LVM report lacks {name}")
        pv = pvs[0]
        pool = rows["pool"]
        disk = rows["disk-duranium"]
        pv_vg_uuid = str(pv.get("vg_uuid", "")).strip()
        if any(
            str(rows[name].get("vg_uuid", "")).strip() != pv_vg_uuid
            for name in ("pool", "pool_tdata", "pool_tmeta", "disk-duranium")
        ):
            fail("live pool/disk rows do not all belong to the userdata PV's exact VG")
        active_raw = str(disk.get("lv_active", "")).strip().lower()
        active = active_raw in {"1", "active", "y", "yes"}
        if active_raw not in {"0", "inactive", "", "n", "no", "1", "active", "y", "yes"}:
            fail("live disk activation field is malformed")
        permission_raw = str(disk.get("lv_permissions", "")).strip().lower()
        if permission_raw in {"writeable", "writable", "rw"}:
            permission = "rw"
        elif permission_raw in {"read-only", "readonly", "r"}:
            permission = "r"
        else:
            fail("live disk permission field is malformed")
        dm_uuid: str | None = None
        sectors: int | None = None
        kernel_ro: bool | None = None
        quiescent = True
        if active:
            script = (
                "set -eu; p=/dev/mapper/franken-disk--duranium; n=$(/bin/readlink -f \"$p\"); "
                "case $n in /dev/dm-[0-9]*) ;; *) exit 41;; esac; d=${n##*/}; "
                "s=/sys/class/block/$d; q=1; mm=$(/bin/cat $s/dev); "
                "if /bin/busybox grep -q \" $mm \" /proc/self/mountinfo; then q=0; fi; "
                "while read -r swap rest; do case $swap in Filename) continue;; esac; "
                "r=$(/bin/readlink -f \"$swap\" 2>/dev/null || /bin/true); test \"$r\" = \"$n\" && q=0; done </proc/swaps; "
                "for h in $s/holders/*; do test -e \"$h\" && q=0; done; "
                "for f in /sys/kernel/config/usb_gadget/*/functions/mass_storage.*/lun.*/file; do "
                "test -f \"$f\" || continue; raw=$(/bin/cat \"$f\"); test -n \"$raw\" || continue; "
                "r=$(/bin/readlink -f \"$raw\" 2>/dev/null || /bin/true); test \"$r\" = \"$n\" && q=0; done; "
                "printf '%s|%s|%s|%s\\n' \"$(/bin/cat $s/dm/uuid)\" "
                "\"$(/bin/cat $s/size)\" \"$(/bin/cat $s/ro)\" \"$q\""
            )
            identity = self.run(["/bin/sh", "-c", script])
            _require_remote_success(identity, "active Duranium mapping identity")
            fields = identity.stdout.decode("ascii", "replace").strip().split("|")
            if len(fields) != 4:
                fail("active Duranium mapping identity output is malformed")
            dm_uuid = fields[0]
            sectors = checked_int(fields[1], "active mapping sectors")
            if fields[2] not in {"0", "1"}:
                fail("active mapping read-only field is malformed")
            kernel_ro = fields[2] == "1"
            if fields[3] not in {"0", "1"}:
                fail("active mapping quiescence field is malformed")
            quiescent = fields[3] == "1"
        health = str(pool.get("lv_health_status", "")).strip().lower()
        pool_attr = str(pool.get("lv_attr", "")).strip()
        return LiveState(
            serial=self.serial,
            partuuid=binding.partuuid,
            anchor=self._current_anchor,
            pv_uuid=str(pv.get("pv_uuid", "")).strip(),
            vg_uuid=str(pv.get("vg_uuid", "")).strip(),
            pool_uuid=str(pool.get("lv_uuid", "")).strip(),
            pool_data_uuid=str(pool.get("data_lv_uuid", "")).strip(),
            pool_metadata_uuid=str(pool.get("metadata_lv_uuid", "")).strip(),
            pool_bytes=checked_int(pool.get("lv_size"), "live pool size"),
            pool_chunk_bytes=checked_int(pool.get("chunk_size"), "live pool chunk size"),
            pool_segtype=str(pool.get("segtype", "")).strip(),
            pool_data_percent=str(pool.get("data_percent", "")).strip(),
            pool_metadata_percent=str(pool.get("metadata_percent", "")).strip(),
            pool_healthy=health in {"", "healthy"} and "p" not in pool_attr.lower(),
            pool_discards=str(pool.get("discards", "")).strip(),
            pool_when_full=str(pool.get("lv_when_full", "")).strip(),
            disk_uuid=str(disk.get("lv_uuid", "")).strip(),
            disk_bytes=checked_int(disk.get("lv_size"), "live disk size"),
            disk_segtype=str(disk.get("segtype", "")).strip(),
            disk_pool_uuid=str(disk.get("pool_lv_uuid", "")).strip(),
            disk_tags=_tags(disk.get("lv_tags"), "live disk tags"),
            disk_permission=permission,
            disk_active=active,
            disk_dm_uuid=dm_uuid,
            disk_sectors=sectors,
            disk_kernel_ro=kernel_ro,
            disk_quiescent=quiescent,
        )

    def _lvm(self, binding: Binding, applet: str, *arguments: str) -> RemoteResult:
        return self.run(
            [
                "/sbin/lvm.static", applet, "--devices", binding.anchor, "--nohints",
                "--config", LVM_CONFIG, *arguments,
            ]
        )

    def activate_writable(self, binding: Binding) -> RemoteResult:
        return self.run(activate_writable_argv(binding), timeout=300)

    def write_extent(self, binding: Binding, disk: HeldFile, extent: Extent) -> RemoteResult:
        with tempfile.TemporaryFile() as stream:
            remaining = extent.source_bytes
            position = extent.source_offset
            while remaining:
                block = os.pread(disk.fd, min(1024 * 1024, remaining), position)
                if not block:
                    fail(f"held disk ended while staging extent {extent.ordinal}")
                stream.write(block)
                remaining -= len(block)
                position += len(block)
            stream.flush()
            stream.seek(0)
            return self.run(
                write_extent_argv(binding, extent), input_file=stream, timeout=300
            )

    def verify_extent(self, binding: Binding, extent: Extent) -> RemoteResult:
        return self.run(verify_extent_argv(binding, extent), timeout=300)

    def sync(self, binding: Binding) -> RemoteResult:
        return self.run(sync_import_argv(binding), timeout=300)

    def make_readonly(self, binding: Binding) -> RemoteResult:
        return self.run(make_readonly_argv(binding), timeout=300)

    def attest(self, binding: Binding, expected_sha256: str) -> tuple[RemoteResult, dict[str, object]]:
        result = self.run(attest_argv(binding, expected_sha256), timeout=None)
        expected_line = f"FRANKENSARGO_DURANIUM_SHA256_V1|{expected_sha256}\n".encode()
        if (
            not result.trustworthy_remote_status
            or result.returncode != 0
            or result.stdout != expected_line
            or result.stderr
        ):
            # Do not parse or throw away a typed failure here.  The controller
            # must durably journal its exact argv/status/output evidence before
            # rejecting it, otherwise restart cannot distinguish a received
            # nonzero from a transport disconnect.  Success-shaped malformed
            # output follows the same durable fail-closed path.
            return result, {
                "schema": ATTEST_SCHEMA,
                "remote_success": False,
                "stdout_bytes": len(result.stdout),
                "stdout_sha256": sha256_bytes(result.stdout),
                "stderr_bytes": len(result.stderr),
                "stderr_sha256": sha256_bytes(result.stderr),
            }
        return result, {
            "schema": ATTEST_SCHEMA,
            "serial": binding.serial,
            "userdata_partuuid": binding.partuuid,
            "vg_uuid": binding.vg_uuid,
            "lv_uuid": binding.disk_uuid,
            "lvm_dm_uuid": binding.disk_dm_uuid,
            "sectors": binding.disk_sectors,
            "bytes": binding.disk_bytes,
            "ro": True,
            "sha_applet": "/bin/busybox",
            "expected_sha256": expected_sha256,
            "actual_sha256": expected_sha256,
        }

    def deactivate(self, binding: Binding) -> RemoteResult:
        return self.run(deactivate_argv(binding), timeout=300)

    def publish(self, binding: Binding) -> RemoteResult:
        return self.run(publish_argv(binding), timeout=300)

    def capture_vgcfg(self, binding: Binding) -> tuple[RemoteResult, bytes]:
        # One shell-v2 result covers both successful metadata capture and the
        # exact bytes returned to the host; vgcfgbackup chatter is kept off the
        # file-byte stdout channel.
        result = self.run(capture_vgcfg_argv(binding), timeout=300)
        return result, result.stdout if result.trustworthy_remote_status and result.returncode == 0 else b""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="execute-duranium-import",
        description="Run one crash-resumable, hash-gated sparse Duranium import transaction.",
    )
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--bootstrap-checkpoint", required=True, type=Path)
    parser.add_argument("--audit", required=True, type=Path)
    parser.add_argument("--disk", required=True, type=Path)
    parser.add_argument("--provenance", required=True, type=Path)
    parser.add_argument("--adapter", required=True, type=Path)
    parser.add_argument("--pocketboot-image", required=True, type=Path)
    parser.add_argument("--pocketboot-provenance", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--partuuid", required=True)
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--confirm")
    parser.add_argument("--print-confirmation", action="store_true")
    parser.add_argument("--execute", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not SERIAL_RE.fullmatch(args.serial):
            fail("serial contains unsafe characters")
        partuuid = checked_uuid(args.partuuid, "armed userdata PARTUUID")
        held: list[HeldFile] = []
        journal: Journal | None = None
        try:
            plan = HeldFile.open("bootstrap plan", args.plan, MAX_JSON); held.append(plan)
            checkpoint = HeldFile.open("final bootstrap checkpoint", args.bootstrap_checkpoint, MAX_JSON); held.append(checkpoint)
            audit_file = HeldFile.open("provided Duranium audit", args.audit, MAX_JSON); held.append(audit_file)
            disk = HeldFile.open("derived disk", args.disk); held.append(disk)
            provenance = HeldFile.open("derived provenance", args.provenance, MAX_JSON); held.append(provenance)
            adapter = HeldFile.open("Duranium adapter", args.adapter, 256 * 1024 * 1024); held.append(adapter)
            pocketboot_image = HeldFile.open("bound PocketBoot image", args.pocketboot_image, 256 * 1024 * 1024); held.append(pocketboot_image)
            pocketboot_provenance = HeldFile.open("bound PocketBoot provenance", args.pocketboot_provenance, MAX_JSON); held.append(pocketboot_provenance)
            repository_root = Path(__file__).resolve().parents[1]
            audit_tool_path = repository_root / "bin/audit-duranium-import"
            audit_tool = HeldFile.open("audit_tool", audit_tool_path, MAX_JSON); held.append(audit_tool)
            entrypoint = HeldFile.open(
                "entrypoint", repository_root / "bin/execute-duranium-import", MAX_JSON
            ); held.append(entrypoint)
            controller_implementation = HeldFile.open(
                "controller", Path(__file__).resolve(), MAX_JSON
            ); held.append(controller_implementation)
            shell_v2_implementation = HeldFile.open(
                "shell_v2", repository_root / "lib/adb_shell_v2.py", MAX_JSON
            ); held.append(shell_v2_implementation)
            implementation_files = (
                entrypoint, controller_implementation, shell_v2_implementation, audit_tool
            )
            implementation_hashes = {
                item.field: item.sha256 for item in implementation_files
            }
            provided_audit = parse_json(audit_file.read(), "provided Duranium audit")
            binding = validate_plan_checkpoint(plan, checkpoint, args.serial, partuuid, provided_audit)
            pocketboot_runtime = validate_pocketboot_runtime(
                pocketboot_image,
                pocketboot_provenance,
                binding,
                Path(__file__).resolve().parents[1] / "patches/pocketboot",
            )
            contract = independently_reaudit(
                provided_audit, binding, disk, provenance, adapter, audit_tool
            )
            confirmation_digest = sha256_bytes(
                canonical_bytes(
                    {
                        "audit_sha256": contract.sha256,
                        "audit_tool_sha256": audit_tool.sha256,
                        "implementation_sha256": implementation_hashes,
                        "pocketboot_image_sha256": pocketboot_image.sha256,
                        "pocketboot_provenance_sha256": pocketboot_provenance.sha256,
                    }
                )
            )
            token = f"IMPORT-DURANIUM-{binding.operation_uuid.split('-', 1)[0]}-{confirmation_digest[:12]}"
            if args.print_confirmation:
                if args.execute:
                    fail("--print-confirmation and --execute are mutually exclusive")
                print(token)
                return 0
            if not args.execute:
                fail("refusing to contact ADB or create state without literal --execute")
            if args.confirm != token:
                fail(f"confirmation token mismatch; run --print-confirmation against the exact frozen inputs")
            state_dir = args.state_dir.expanduser()
            if not state_dir.is_absolute():
                fail("state directory must be absolute")
            journal = Journal(state_dir.resolve(strict=False))
            archived_audit = journal.root / "audit.json"
            audit_bytes = canonical_bytes(contract.value)
            if archived_audit.exists():
                existing = HeldFile.open("archived import audit", archived_audit, MAX_JSON)
                try:
                    if existing.read() != audit_bytes:
                        fail("durable archived audit differs from the fresh independent audit")
                finally:
                    existing.close()
            else:
                atomic_write(archived_audit, audit_bytes)
            remote = AdbShellV2Remote(args.adb, args.serial)
            controller = ImportController(
                binding=binding,
                contract=contract,
                disk=disk,
                provenance=provenance,
                adapter=adapter,
                pocketboot_image=pocketboot_image,
                pocketboot_provenance=pocketboot_provenance,
                pocketboot_runtime=pocketboot_runtime,
                remote=remote,
                journal=journal,
                audit_tool_sha256=audit_tool.sha256,
                implementation_hashes=implementation_hashes,
                implementation_files=implementation_files,
            )
            print(json.dumps(controller.execute(), indent=2, sort_keys=True))
            return 0
        finally:
            if journal is not None:
                journal.close()
            for item in reversed(held):
                item.close()
    except ImportFailure as error:
        print(f"execute-duranium-import: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
