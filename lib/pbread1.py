"""Resumable, host-side PBREAD1 raw partition backup support.

Only the FastbootTransport talks to a device.  The rest of this module is
deliberately ordinary-file code so the safety and resume machinery can be
tested without USB or root privileges.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import uuid
from typing import BinaryIO, Callable, Iterator, Sequence


HEADER_BYTES = 512
MAGIC = b"PBREAD1\0"
FLAG_PAYLOAD = 1
FLAG_HASH_ONLY = 2
DEFAULT_CHUNK_BYTES = 64 * 1024 * 1024
COPY_BYTES = 1024 * 1024
MANIFEST_SCHEMA = "org.frankensargo.pbread1-backup/1"
JOURNAL_SCHEMA = "org.frankensargo.pbread1-journal/1"
SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
SERIAL_RE = re.compile(r"^[A-Za-z0-9._:][A-Za-z0-9._:-]*$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9._+-]+$")


class BackupError(RuntimeError):
    """A fail-closed backup or validation error."""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(COPY_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def pretty_json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def normalized_sha256(value: str, field: str) -> str:
    match = SHA256_RE.fullmatch(value)
    if not match:
        raise BackupError(f"{field} is not a lowercase SHA-256")
    return match.group(1)


def normalized_uuid(value: str, field: str, *, allow_zero: bool = False) -> tuple[str, str]:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as error:
        raise BackupError(f"{field} is not a UUID: {value!r}") from error
    if parsed.int == 0 and not allow_zero:
        raise BackupError(f"{field} must not be the zero UUID")
    return str(parsed), parsed.hex


def decimal(value: object, field: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0|[1-9][0-9]*", value):
        raise BackupError(f"{field} is not a canonical decimal string")
    return int(value, 10)


@dataclasses.dataclass(frozen=True)
class PartitionBinding:
    partuuid: str
    partuuid32: str
    parttype: str
    parttype32: str
    partlabel: str
    kernel_name: str
    start_lba: int
    sectors: int
    logical_sector_bytes: int
    partition_bytes: int

    @classmethod
    def from_manifest(cls, manifest: dict[str, object]) -> "PartitionBinding":
        partition = require_dict(manifest.get("partition"), "manifest.partition")
        partuuid, partuuid32 = normalized_uuid(
            require_str(partition.get("partuuid"), "partuuid"),
            "partuuid",
        )
        parttype, parttype32 = normalized_uuid(
            require_str(partition.get("type_guid"), "type_guid"),
            "type_guid",
        )
        if partition.get("partuuid32") != partuuid32:
            raise BackupError("manifest PARTUUID representations disagree")
        if partition.get("type_guid32") != parttype32:
            raise BackupError("manifest type GUID representations disagree")
        binding = cls(
            partuuid=partuuid,
            partuuid32=partuuid32,
            parttype=parttype,
            parttype32=parttype32,
            partlabel=require_str(partition.get("partlabel"), "partlabel"),
            kernel_name=require_str(partition.get("kernel_name_observation"), "kernel_name_observation"),
            start_lba=decimal(partition.get("start_lba"), "start_lba"),
            sectors=decimal(partition.get("sectors"), "sectors"),
            logical_sector_bytes=require_int(partition.get("logical_sector_bytes"), "logical_sector_bytes"),
            partition_bytes=decimal(partition.get("raw_bytes"), "raw_bytes"),
        )
        if not SAFE_LABEL_RE.fullmatch(binding.partlabel):
            raise BackupError("manifest partlabel is unsafe")
        if not re.fullmatch(r"mmcblk[0-9]+p[1-9][0-9]*", binding.kernel_name):
            raise BackupError("manifest kernel partition observation is malformed")
        if binding.logical_sector_bytes not in (512, 1024, 2048, 4096):
            raise BackupError("manifest logical sector size is unsupported")
        if binding.sectors <= 0 or binding.partition_bytes != binding.sectors * binding.logical_sector_bytes:
            raise BackupError("manifest partition geometry is inconsistent")
        return binding


@dataclasses.dataclass(frozen=True)
class Header:
    flags: int
    partuuid32: str
    parttype32: str
    start_lba: int
    sectors: int
    logical_sector_bytes: int
    partition_bytes: int
    source_offset: int
    source_length: int
    payload_bytes: int
    source_sha256: str
    partlabel: str
    kernel_name: str

    def encode(self) -> bytes:
        if self.flags not in (FLAG_PAYLOAD, FLAG_HASH_ONLY):
            raise BackupError(f"unsupported PBREAD1 flags: {self.flags}")
        output = bytearray(HEADER_BYTES)
        output[0:8] = MAGIC
        struct.pack_into("<II", output, 0x008, HEADER_BYTES, self.flags)
        output[0x010:0x030] = fixed_ascii(self.partuuid32, 32, "partuuid32", nul_terminated=False)
        output[0x030:0x050] = fixed_ascii(self.parttype32, 32, "parttype32", nul_terminated=False)
        struct.pack_into(
            "<QQIIQQQQ",
            output,
            0x050,
            self.start_lba,
            self.sectors,
            self.logical_sector_bytes,
            0,
            self.partition_bytes,
            self.source_offset,
            self.source_length,
            self.payload_bytes,
        )
        output[0x088:0x0A8] = bytes.fromhex(normalized_sha256(self.source_sha256, "source_sha256"))
        output[0x0A8:0x0E8] = fixed_ascii(self.partlabel, 64, "partlabel", nul_terminated=True)
        output[0x0E8:0x108] = fixed_ascii(self.kernel_name, 32, "kernel_name", nul_terminated=True)
        return bytes(output)

    @classmethod
    def decode(cls, raw: bytes) -> "Header":
        if len(raw) != HEADER_BYTES:
            raise BackupError(f"PBREAD1 header is {len(raw)} bytes, expected {HEADER_BYTES}")
        if raw[0:8] != MAGIC:
            raise BackupError("PBREAD1 magic mismatch")
        header_bytes, flags = struct.unpack_from("<II", raw, 0x008)
        if header_bytes != HEADER_BYTES:
            raise BackupError(f"PBREAD1 header size is {header_bytes}, expected {HEADER_BYTES}")
        if flags not in (FLAG_PAYLOAD, FLAG_HASH_ONLY):
            raise BackupError(f"unsupported PBREAD1 flags: {flags}")
        (
            start_lba,
            sectors,
            logical_sector_bytes,
            reserved,
            partition_bytes,
            source_offset,
            source_length,
            payload_bytes,
        ) = struct.unpack_from("<QQIIQQQQ", raw, 0x050)
        if reserved != 0:
            raise BackupError("PBREAD1 reserved word is nonzero")
        if any(raw[0x108:]):
            raise BackupError("PBREAD1 reserved tail is nonzero")
        return cls(
            flags=flags,
            partuuid32=read_fixed_ascii(raw[0x010:0x030], "partuuid32", nul_terminated=False),
            parttype32=read_fixed_ascii(raw[0x030:0x050], "parttype32", nul_terminated=False),
            start_lba=start_lba,
            sectors=sectors,
            logical_sector_bytes=logical_sector_bytes,
            partition_bytes=partition_bytes,
            source_offset=source_offset,
            source_length=source_length,
            payload_bytes=payload_bytes,
            source_sha256=raw[0x088:0x0A8].hex(),
            partlabel=read_fixed_ascii(raw[0x0A8:0x0E8], "partlabel", nul_terminated=True),
            kernel_name=read_fixed_ascii(raw[0x0E8:0x108], "kernel_name", nul_terminated=True),
        )


def fixed_ascii(value: str, size: int, field: str, *, nul_terminated: bool) -> bytes:
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise BackupError(f"{field} must be ASCII") from error
    limit = size - 1 if nul_terminated else size
    if not encoded or len(encoded) > limit:
        raise BackupError(f"{field} must contain 1..{limit} ASCII bytes")
    if b"\0" in encoded:
        raise BackupError(f"{field} contains NUL")
    if not nul_terminated and len(encoded) != size:
        raise BackupError(f"{field} must contain exactly {size} ASCII bytes")
    return encoded.ljust(size, b"\0")


def read_fixed_ascii(raw: bytes, field: str, *, nul_terminated: bool) -> str:
    if nul_terminated:
        value, separator, padding = raw.partition(b"\0")
        if not separator or any(padding):
            raise BackupError(f"{field} has invalid NUL padding")
    else:
        value = raw
        if b"\0" in value:
            raise BackupError(f"{field} unexpectedly contains NUL")
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError as error:
        raise BackupError(f"{field} is not ASCII") from error
    if not decoded:
        raise BackupError(f"{field} is empty")
    return decoded


def require_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BackupError(f"{field} is not an object")
    return value


def require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise BackupError(f"{field} is not an array")
    return value


def require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise BackupError(f"{field} is not a nonempty string")
    return value


def require_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise BackupError(f"{field} is not an integer")
    return value


def verified_inventory(path: Path) -> dict[str, object]:
    try:
        inventory = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"cannot read inventory {path}: {error}") from error
    inventory = require_dict(inventory, "inventory")
    if inventory.get("schema") != "org.frankensargo.inventory/1":
        raise BackupError("inventory schema is not org.frankensargo.inventory/1")
    recorded = normalized_sha256(
        require_str(inventory.get("canonical_sha256"), "canonical_sha256"),
        "canonical_sha256",
    )
    hash_input = dict(inventory)
    del hash_input["canonical_sha256"]
    actual = sha256_bytes(canonical_json_bytes(hash_input))
    if actual != recorded:
        raise BackupError(f"inventory canonical hash mismatch: recorded {recorded}, calculated {actual}")
    return inventory


def manifest_from_inputs(
    inventory_path: Path,
    serial: str,
    requested_partuuid: str,
    requested_partlabel: str,
    pocketboot_image: Path,
    chunk_bytes: int,
    *,
    run_uuid: str | None = None,
    created_at: str | None = None,
) -> dict[str, object]:
    if not SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
        raise BackupError("serial contains unsafe characters")
    if not SAFE_LABEL_RE.fullmatch(requested_partlabel):
        raise BackupError("partlabel contains unsafe characters")
    if chunk_bytes <= 0 or chunk_bytes % 512 != 0 or chunk_bytes > DEFAULT_CHUNK_BYTES:
        raise BackupError(f"chunk size must be a positive 512-byte multiple no larger than {DEFAULT_CHUNK_BYTES}")
    inventory = verified_inventory(inventory_path)
    device = require_dict(inventory.get("device"), "inventory.device")
    if device.get("product") != "sargo":
        raise BackupError("inventory product is not sargo")
    compatibles = require_list(device.get("compatible"), "inventory.device.compatible")
    if "google,sargo" not in compatibles:
        raise BackupError("inventory compatible does not include google,sargo")
    inventory_serial = require_str(device.get("adb_serial"), "inventory.device.adb_serial")
    if inventory_serial != serial:
        raise BackupError(f"requested serial {serial!r} does not match inventory serial {inventory_serial!r}")
    emmc = require_dict(device.get("emmc"), "inventory.device.emmc")
    cid = require_str(emmc.get("cid"), "inventory.device.emmc.cid")
    if not re.fullmatch(r"[0-9a-f]{32}", cid):
        raise BackupError("inventory eMMC CID is malformed")
    logical_sector_bytes = require_int(emmc.get("logical_sector_size"), "logical_sector_size")

    requested_canonical, _ = normalized_uuid(requested_partuuid, "requested partuuid")
    gpt = require_dict(inventory.get("gpt"), "inventory.gpt")
    if gpt.get("backup_entry_array_layout") != "aliases-primary":
        raise BackupError("inventory does not record frankensargo's aliases-primary backup GPT layout")
    if gpt.get("backup_entry_array_independent") is not False:
        raise BackupError("inventory backup GPT independence flag is inconsistent")
    matches = []
    for candidate in require_list(gpt.get("partitions"), "inventory.gpt.partitions"):
        partition = require_dict(candidate, "inventory partition")
        candidate_uuid, _ = normalized_uuid(
            require_str(partition.get("partuuid"), "partition.partuuid"),
            "partition.partuuid",
        )
        if candidate_uuid == requested_canonical:
            matches.append(partition)
    if len(matches) != 1:
        raise BackupError(f"inventory contains {len(matches)} partitions with PARTUUID {requested_canonical}")
    partition = matches[0]
    partlabel = require_str(partition.get("name"), "partition.name")
    if partlabel != requested_partlabel:
        raise BackupError(f"PARTUUID is {partlabel!r}, not requested label {requested_partlabel!r}")
    if not SAFE_LABEL_RE.fullmatch(partlabel):
        raise BackupError("inventory partition label is unsafe for a backup filename")
    kernel_name = require_str(partition.get("kernel_node_observation"), "partition.kernel_node_observation")
    if not re.fullmatch(r"mmcblk[0-9]+p[1-9][0-9]*", kernel_name):
        raise BackupError("inventory kernel partition observation is malformed")
    partuuid, partuuid32 = normalized_uuid(require_str(partition.get("partuuid"), "partuuid"), "partuuid")
    parttype, parttype32 = normalized_uuid(require_str(partition.get("type_guid"), "type_guid"), "type_guid")
    start_lba = decimal(partition.get("start_lba"), "partition.start_lba")
    sectors = decimal(partition.get("sector_count"), "partition.sector_count")
    last_lba = decimal(partition.get("last_lba"), "partition.last_lba")
    raw_bytes = decimal(partition.get("byte_size"), "partition.byte_size")
    if sectors <= 0 or last_lba != start_lba + sectors - 1:
        raise BackupError("inventory partition LBA geometry is inconsistent")
    if raw_bytes != sectors * logical_sector_bytes:
        raise BackupError("inventory partition byte size is inconsistent")
    disk_guid, _ = normalized_uuid(
        require_str(gpt.get("disk_guid"), "gpt.disk_guid"),
        "gpt.disk_guid",
        allow_zero=True,
    )
    if disk_guid != "00000000-0000-0000-0000-000000000000" or gpt.get("disk_guid_is_zero") is not True:
        raise BackupError("inventory does not record frankensargo's observed zero GPT disk GUID")

    if not pocketboot_image.is_file():
        raise BackupError(f"PocketBoot image is not a file: {pocketboot_image}")
    pocketboot_bytes = pocketboot_image.stat().st_size
    if pocketboot_bytes <= 0:
        raise BackupError("PocketBoot image is empty")
    pocketboot_sha256 = sha256_file(pocketboot_image)
    inventory_sha256 = normalized_sha256(
        require_str(inventory.get("canonical_sha256"), "canonical_sha256"),
        "canonical_sha256",
    )
    entry_array_sha256 = normalized_sha256(
        require_str(gpt.get("entry_array_sha256"), "gpt.entry_array_sha256"),
        "gpt.entry_array_sha256",
    )
    chunk_count = len(plan_chunks(raw_bytes, chunk_bytes))

    return {
        "schema": MANIFEST_SCHEMA,
        "run_uuid": run_uuid or str(uuid.uuid4()),
        "created_at_utc": created_at or utc_now(),
        "device": {
            "product": "sargo",
            "compatible": "google,sargo",
            "fastboot_serial": serial,
            "emmc_cid": cid,
            "gpt_disk_guid": disk_guid,
        },
        "inventory": {
            "canonical_sha256": f"sha256:{inventory_sha256}",
            "entry_array_sha256": f"sha256:{entry_array_sha256}",
            "backup_entry_array_layout": require_str(
                gpt.get("backup_entry_array_layout"),
                "backup_entry_array_layout",
            ),
        },
        "partition": {
            "partuuid": partuuid,
            "partuuid32": partuuid32,
            "type_guid": parttype,
            "type_guid32": parttype32,
            "partlabel": partlabel,
            "kernel_name_observation": kernel_name,
            "start_lba": str(start_lba),
            "sectors": str(sectors),
            "logical_sector_bytes": logical_sector_bytes,
            "raw_bytes": str(raw_bytes),
        },
        "pocketboot": {
            "image_name": pocketboot_image.name,
            "image_bytes": str(pocketboot_bytes),
            "image_sha256": f"sha256:{pocketboot_sha256}",
        },
        "transport": {
            "protocol": "PBREAD1",
            "header_bytes": HEADER_BYTES,
            "chunk_bytes": chunk_bytes,
            "chunk_count": chunk_count,
        },
    }


def manifest_binding(manifest: dict[str, object]) -> dict[str, object]:
    return {
        key: manifest[key]
        for key in ("schema", "device", "inventory", "partition", "pocketboot", "transport")
    }


def validate_run_manifest(manifest: dict[str, object]) -> PartitionBinding:
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise BackupError("run manifest schema mismatch")
    try:
        uuid.UUID(require_str(manifest.get("run_uuid"), "run_uuid"))
    except ValueError as error:
        raise BackupError("run_uuid is malformed") from error
    require_str(manifest.get("created_at_utc"), "created_at_utc")
    device = require_dict(manifest.get("device"), "manifest.device")
    if device.get("product") != "sargo" or device.get("compatible") != "google,sargo":
        raise BackupError("run manifest is not bound to google,sargo")
    serial = require_str(device.get("fastboot_serial"), "fastboot_serial")
    if not SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
        raise BackupError("manifest fastboot serial is unsafe")
    if not re.fullmatch(r"[0-9a-f]{32}", require_str(device.get("emmc_cid"), "emmc_cid")):
        raise BackupError("manifest eMMC CID is malformed")
    disk_guid, _ = normalized_uuid(
        require_str(device.get("gpt_disk_guid"), "gpt_disk_guid"),
        "gpt_disk_guid",
        allow_zero=True,
    )
    if disk_guid != "00000000-0000-0000-0000-000000000000":
        raise BackupError("manifest GPT disk GUID is not frankensargo's observed zero GUID")
    inventory = require_dict(manifest.get("inventory"), "manifest.inventory")
    normalized_sha256(
        require_str(inventory.get("canonical_sha256"), "inventory.canonical_sha256"),
        "inventory.canonical_sha256",
    )
    normalized_sha256(
        require_str(inventory.get("entry_array_sha256"), "inventory.entry_array_sha256"),
        "inventory.entry_array_sha256",
    )
    if inventory.get("backup_entry_array_layout") != "aliases-primary":
        raise BackupError("manifest backup GPT layout mismatch")
    pocketboot = require_dict(manifest.get("pocketboot"), "manifest.pocketboot")
    require_str(pocketboot.get("image_name"), "pocketboot.image_name")
    if decimal(pocketboot.get("image_bytes"), "pocketboot.image_bytes") <= 0:
        raise BackupError("manifest PocketBoot image is empty")
    normalized_sha256(
        require_str(pocketboot.get("image_sha256"), "pocketboot.image_sha256"),
        "pocketboot.image_sha256",
    )
    binding = PartitionBinding.from_manifest(manifest)
    transport = require_dict(manifest.get("transport"), "manifest.transport")
    if transport.get("protocol") != "PBREAD1" or transport.get("header_bytes") != HEADER_BYTES:
        raise BackupError("manifest PBREAD1 transport identity mismatch")
    chunk_bytes = require_int(transport.get("chunk_bytes"), "transport.chunk_bytes")
    if (
        chunk_bytes <= 0
        or chunk_bytes > DEFAULT_CHUNK_BYTES
        or chunk_bytes % binding.logical_sector_bytes != 0
    ):
        raise BackupError("manifest chunk size is invalid")
    expected_count = len(plan_chunks(binding.partition_bytes, chunk_bytes))
    if transport.get("chunk_count") != expected_count:
        raise BackupError("manifest chunk count is inconsistent")
    return binding


def plan_chunks(total_bytes: int, chunk_bytes: int) -> list[tuple[int, int]]:
    if total_bytes <= 0:
        raise BackupError("partition size must be positive")
    if chunk_bytes <= 0:
        raise BackupError("chunk size must be positive")
    return [(offset, min(chunk_bytes, total_bytes - offset)) for offset in range(0, total_bytes, chunk_bytes)]


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write(path: Path, contents: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as output:
            output.write(contents)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()


def manifest_file_hash(path: Path) -> str:
    return sha256_file(path)


def write_new_manifest(run_dir: Path, manifest: dict[str, object]) -> str:
    manifest_path = run_dir / "manifest.json"
    checksum_path = run_dir / "manifest.json.sha256"
    if manifest_path.exists() or checksum_path.exists():
        raise BackupError("refusing to overwrite an existing partial run manifest")
    contents = pretty_json_bytes(manifest)
    digest = sha256_bytes(contents)
    atomic_write(manifest_path, contents)
    atomic_write(checksum_path, f"{digest}  manifest.json\n".encode())
    return digest


def load_manifest(run_dir: Path) -> tuple[dict[str, object], str]:
    manifest_path = run_dir / "manifest.json"
    checksum_path = run_dir / "manifest.json.sha256"
    try:
        checksum_words = checksum_path.read_text().split()
        if len(checksum_words) != 2 or checksum_words[1] != "manifest.json":
            raise BackupError("manifest checksum sidecar is malformed")
        expected = normalized_sha256(checksum_words[0], "manifest checksum")
        actual = manifest_file_hash(manifest_path)
        if actual != expected:
            raise BackupError(f"run manifest checksum mismatch: expected {expected}, got {actual}")
        manifest = require_dict(json.loads(manifest_path.read_text()), "run manifest")
    except FileNotFoundError as error:
        raise BackupError(f"run manifest is incomplete: {error.filename} is missing") from error
    except json.JSONDecodeError as error:
        raise BackupError(f"run manifest is invalid JSON: {error}") from error
    validate_run_manifest(manifest)
    return manifest, actual


def new_journal(manifest_sha256: str) -> dict[str, object]:
    return {
        "schema": JOURNAL_SCHEMA,
        "manifest_sha256": f"sha256:{manifest_sha256}",
        "chunks": {},
        "assembled": None,
        "source_verification": None,
        "updated_at_utc": utc_now(),
    }


def load_journal(run_dir: Path, manifest_sha256: str) -> dict[str, object]:
    path = run_dir / "journal.json"
    if not path.exists():
        return new_journal(manifest_sha256)
    try:
        journal = require_dict(json.loads(path.read_text()), "journal")
    except (OSError, json.JSONDecodeError) as error:
        raise BackupError(f"cannot read journal: {error}") from error
    if journal.get("schema") != JOURNAL_SCHEMA:
        raise BackupError("journal schema mismatch")
    recorded = normalized_sha256(
        require_str(journal.get("manifest_sha256"), "journal.manifest_sha256"),
        "journal.manifest_sha256",
    )
    if recorded != manifest_sha256:
        raise BackupError("journal belongs to a different run manifest")
    require_dict(journal.get("chunks"), "journal.chunks")
    return journal


def write_journal(run_dir: Path, journal: dict[str, object]) -> None:
    journal["updated_at_utc"] = utc_now()
    atomic_write(run_dir / "journal.json", pretty_json_bytes(journal))


@contextlib.contextmanager
def run_lock(run_dir: Path, *, exclusive: bool) -> Iterator[None]:
    if exclusive:
        run_dir.mkdir(parents=True, exist_ok=True)
    elif not run_dir.is_dir():
        raise BackupError(f"backup run directory does not exist: {run_dir}")
    lock_path = run_dir / ".lock"
    mode = "a+b" if exclusive else "rb"
    try:
        lock = lock_path.open(mode)
    except FileNotFoundError as error:
        raise BackupError(f"backup run lock is missing: {lock_path}") from error
    with lock:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(lock.fileno(), operation | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BackupError(f"another PBREAD1 operation holds {lock_path}") from error
        yield


def validate_header(header: Header, binding: PartitionBinding, offset: int, length: int, flags: int) -> None:
    expected = {
        "flags": flags,
        "partuuid32": binding.partuuid32,
        "parttype32": binding.parttype32,
        "start_lba": binding.start_lba,
        "sectors": binding.sectors,
        "logical_sector_bytes": binding.logical_sector_bytes,
        "partition_bytes": binding.partition_bytes,
        "source_offset": offset,
        "source_length": length,
        "payload_bytes": length if flags == FLAG_PAYLOAD else 0,
        "partlabel": binding.partlabel,
        "kernel_name": binding.kernel_name,
    }
    for field, wanted in expected.items():
        actual = getattr(header, field)
        if actual != wanted:
            raise BackupError(f"PBREAD1 {field} mismatch: expected {wanted!r}, got {actual!r}")
    if offset < 0 or length <= 0 or offset % binding.logical_sector_bytes or length % binding.logical_sector_bytes:
        raise BackupError("PBREAD1 source range is not positive and sector aligned")
    if offset + length > binding.partition_bytes:
        raise BackupError("PBREAD1 source range exceeds the partition")


def read_header(path: Path) -> Header:
    with path.open("rb") as source:
        raw = source.read(HEADER_BYTES)
    return Header.decode(raw)


def extract_envelope(
    envelope: Path,
    destination: Path | None,
    binding: PartitionBinding,
    offset: int,
    length: int,
    *,
    flags: int = FLAG_PAYLOAD,
) -> str:
    header = read_header(envelope)
    validate_header(header, binding, offset, length, flags)
    expected_size = HEADER_BYTES + header.payload_bytes
    actual_size = envelope.stat().st_size
    if actual_size != expected_size:
        raise BackupError(f"PBREAD1 envelope is {actual_size} bytes, expected {expected_size}")
    if flags == FLAG_HASH_ONLY:
        # A hash-only record has no payload to hash on the host.  Its digest is
        # the independently computed full-source observation made by
        # PocketBoot; identity, geometry, and exact record length are still
        # validated above.
        return header.source_sha256
    digest = hashlib.sha256()
    output: BinaryIO | None = None
    try:
        if destination is not None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            output = destination.open("xb")
        with envelope.open("rb") as source:
            source.seek(HEADER_BYTES)
            remaining = header.payload_bytes
            while remaining:
                chunk = source.read(min(COPY_BYTES, remaining))
                if not chunk:
                    raise BackupError("PBREAD1 payload ended early")
                digest.update(chunk)
                if output is not None:
                    output.write(chunk)
                remaining -= len(chunk)
            if source.read(1):
                raise BackupError("PBREAD1 envelope has trailing bytes")
        actual_digest = digest.hexdigest()
        if actual_digest != header.source_sha256:
            raise BackupError(
                f"PBREAD1 payload SHA-256 mismatch: device {header.source_sha256}, host {actual_digest}"
            )
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
        return actual_digest
    except Exception:
        if output is not None:
            output.close()
        if destination is not None:
            with contextlib.suppress(FileNotFoundError):
                destination.unlink()
        raise
    finally:
        if output is not None and not output.closed:
            output.close()


def header_for_range(binding: PartitionBinding, offset: int, length: int, digest: str) -> Header:
    return Header(
        flags=FLAG_PAYLOAD,
        partuuid32=binding.partuuid32,
        parttype32=binding.parttype32,
        start_lba=binding.start_lba,
        sectors=binding.sectors,
        logical_sector_bytes=binding.logical_sector_bytes,
        partition_bytes=binding.partition_bytes,
        source_offset=offset,
        source_length=length,
        payload_bytes=length,
        source_sha256=digest,
        partlabel=binding.partlabel,
        kernel_name=binding.kernel_name,
    )


def header_for_hash(binding: PartitionBinding, digest: str) -> Header:
    return dataclasses.replace(
        header_for_range(binding, 0, binding.partition_bytes, digest),
        flags=FLAG_HASH_ONLY,
        payload_bytes=0,
    )


class Transport:
    def preflight(self, manifest: dict[str, object], binding: PartitionBinding) -> None:
        raise NotImplementedError

    def stage_range(self, binding: PartitionBinding, offset: int, length: int, destination: Path) -> None:
        raise NotImplementedError

    def stage_hash(self, binding: PartitionBinding, destination: Path) -> None:
        raise NotImplementedError


class FastbootTransport(Transport):
    def __init__(
        self,
        executable: str,
        serial: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        if not SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
            raise BackupError("serial contains unsafe characters")
        resolved = shutil.which(executable)
        if resolved is None:
            raise BackupError(f"fastboot executable was not found: {executable}")
        self.executable = resolved
        self.serial = serial
        self.runner = runner or subprocess.run

    def _run(self, arguments: Sequence[str]) -> str:
        command = [self.executable, "-s", self.serial, *arguments]
        result = self.runner(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise BackupError(f"fastboot command failed ({' '.join(arguments)}):\n{result.stdout.rstrip()}")
        return result.stdout

    def _getvar(self, name: str) -> str:
        output = self._run(["getvar", name])
        pattern = re.compile(rf"(?:\(bootloader\)\s*)?{re.escape(name)}:\s*(\S+)")
        values = set(pattern.findall(output))
        if len(values) != 1:
            raise BackupError(f"fastboot getvar {name!r} was ambiguous or missing:\n{output.rstrip()}")
        return values.pop()

    def preflight(self, manifest: dict[str, object], binding: PartitionBinding) -> None:
        device = require_dict(manifest.get("device"), "manifest.device")
        expected_serial = require_str(device.get("fastboot_serial"), "fastboot_serial")
        if expected_serial != self.serial:
            raise BackupError("transport serial does not match the run manifest")
        if self._getvar("serialno") != self.serial:
            raise BackupError("fastboot-reported serial does not match the run manifest")
        if self._getvar("product") != "pocketboot":
            raise BackupError("fastboot endpoint is not PocketBoot")
        if "sargo" not in self._getvar("compatible"):
            raise BackupError("PocketBoot compatible does not identify sargo")
        size_value = self._getvar(f"partition-size:{binding.partlabel}")
        try:
            size = int(size_value, 0)
        except ValueError as error:
            raise BackupError(f"fastboot partition size is not an integer: {size_value!r}") from error
        if size != binding.partition_bytes:
            raise BackupError(f"fastboot partition size mismatch: expected {binding.partition_bytes}, got {size}")

    def stage_range(self, binding: PartitionBinding, offset: int, length: int, destination: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        arguments = ["oem", "read", binding.partuuid32, f"{offset:x}", f"{length:x}"]
        wire_command = " ".join(arguments)
        if len(wire_command.encode("ascii")) > 64:
            raise BackupError(f"PBREAD1 command exceeds fastboot's 64-byte command limit: {wire_command}")
        self._run(arguments)
        self._run(["get_staged", str(destination)])
        if not destination.is_file():
            raise BackupError("fastboot get_staged produced no envelope")

    def stage_hash(self, binding: PartitionBinding, destination: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        self._run(["oem", "hash", binding.partuuid32])
        self._run(["get_staged", str(destination)])
        if not destination.is_file():
            raise BackupError("fastboot get_staged produced no hash record")


class OfflineTransport(Transport):
    def __init__(self, source: Path) -> None:
        if not source.is_file():
            raise BackupError(f"offline source is not a file: {source}")
        self.source = source

    def preflight(self, manifest: dict[str, object], binding: PartitionBinding) -> None:
        del manifest
        if self.source.stat().st_size != binding.partition_bytes:
            raise BackupError(
                f"offline source is {self.source.stat().st_size} bytes, expected {binding.partition_bytes}"
            )

    def stage_range(self, binding: PartitionBinding, offset: int, length: int, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        digest = hashlib.sha256()
        with destination.open("xb") as output, self.source.open("rb") as source:
            output.write(bytes(HEADER_BYTES))
            source.seek(offset)
            remaining = length
            while remaining:
                chunk = source.read(min(COPY_BYTES, remaining))
                if not chunk:
                    raise BackupError("offline source ended during range capture")
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            output.seek(0)
            output.write(header_for_range(binding, offset, length, digest.hexdigest()).encode())
            output.flush()
            os.fsync(output.fileno())

    def stage_hash(self, binding: PartitionBinding, destination: Path) -> None:
        digest = sha256_file(self.source)
        atomic_write(destination, header_for_hash(binding, digest).encode())


def chunk_key(index: int) -> str:
    return f"{index:08d}"


def chunk_path(run_dir: Path, index: int) -> Path:
    return run_dir / "chunks" / f"{chunk_key(index)}.bin"


def matching_journal_chunk(entry: object, offset: int, length: int) -> str | None:
    if not isinstance(entry, dict):
        return None
    if entry.get("offset") != str(offset) or entry.get("length") != str(length):
        return None
    try:
        return normalized_sha256(require_str(entry.get("sha256"), "chunk.sha256"), "chunk.sha256")
    except BackupError:
        return None


def existing_chunk_digest(path: Path, expected_length: int, expected_digest: str) -> str | None:
    if not path.is_file() or path.stat().st_size != expected_length:
        return None
    actual = sha256_file(path)
    return actual if actual == expected_digest else None


def quarantine(run_dir: Path, path: Path, reason: str) -> None:
    if not path.exists():
        return
    rejected = run_dir / "rejected"
    rejected.mkdir(parents=True, exist_ok=True)
    destination = rejected / f"{path.name}.{utc_now().replace(':', '')}.{uuid.uuid4().hex[:8]}"
    os.replace(path, destination)
    fsync_directory(path.parent)
    fsync_directory(rejected)
    print(f"quarantined {path.name}: {reason}", file=sys.stderr)


def ensure_free_space(
    run_dir: Path,
    binding: PartitionBinding,
    chunks: list[tuple[int, int]],
    journal: dict[str, object],
) -> None:
    entries = require_dict(journal.get("chunks"), "journal.chunks")
    missing = sum(length for index, (_, length) in enumerate(chunks) if chunk_key(index) not in entries)
    # Keep enough headroom for a fresh assembly even when one already exists.
    # A stale image is quarantined rather than destroyed, so it does not free
    # space for its replacement.
    assembly = binding.partition_bytes
    reserve = 1024 * 1024 * 1024
    required = missing + assembly + max(length for _, length in chunks) + reserve
    available = shutil.disk_usage(run_dir).free
    if available < required:
        raise BackupError(f"insufficient host space: need {required} bytes including reserve, have {available}")


def prepare_manifest(
    run_dir: Path,
    proposed: dict[str, object],
) -> tuple[dict[str, object], str, bool]:
    validate_run_manifest(proposed)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists() or (run_dir / "manifest.json.sha256").exists():
        existing, digest = load_manifest(run_dir)
        if manifest_binding(existing) != manifest_binding(proposed):
            raise BackupError("resume inputs do not match the immutable run manifest")
        return existing, digest, False
    digest = write_new_manifest(run_dir, proposed)
    return proposed, digest, True


def capture_chunks(
    run_dir: Path,
    manifest: dict[str, object],
    manifest_sha256: str,
    transport: Transport,
) -> tuple[dict[str, object], int, int]:
    binding = PartitionBinding.from_manifest(manifest)
    transport_config = require_dict(manifest.get("transport"), "manifest.transport")
    chunk_bytes = require_int(transport_config.get("chunk_bytes"), "chunk_bytes")
    chunks = plan_chunks(binding.partition_bytes, chunk_bytes)
    journal = load_journal(run_dir, manifest_sha256)
    ensure_free_space(run_dir, binding, chunks, journal)
    entries = require_dict(journal.get("chunks"), "journal.chunks")
    downloaded = 0
    skipped = 0
    downloads = run_dir / ".downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    (run_dir / "chunks").mkdir(parents=True, exist_ok=True)

    for index, (offset, length) in enumerate(chunks):
        key = chunk_key(index)
        final = chunk_path(run_dir, index)
        recorded_digest = matching_journal_chunk(entries.get(key), offset, length)
        if recorded_digest and existing_chunk_digest(final, length, recorded_digest):
            skipped += 1
            continue
        if key in entries:
            entries.pop(key)
            write_journal(run_dir, journal)
        if final.exists():
            quarantine(run_dir, final, "not bound by a valid journal entry")

        envelope = downloads / f"{key}.pbr.part"
        temporary = final.with_name(f".{final.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
        try:
            transport.stage_range(binding, offset, length, envelope)
            digest = extract_envelope(envelope, temporary, binding, offset, length)
            os.replace(temporary, final)
            fsync_directory(final.parent)
        finally:
            with contextlib.suppress(FileNotFoundError):
                envelope.unlink()
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()
        entries[key] = {
            "offset": str(offset),
            "length": str(length),
            "sha256": f"sha256:{digest}",
            "verified_at_utc": utc_now(),
        }
        # An assembly is a statement about the complete ordered chunk set.
        # Replacing even one chunk invalidates that statement until a fresh
        # full assembly and source comparison complete.
        journal["assembled"] = None
        journal["source_verification"] = None
        write_journal(run_dir, journal)
        downloaded += 1
        print(f"verified chunk {index + 1}/{len(chunks)} offset=0x{offset:x} length=0x{length:x}")
    return journal, downloaded, skipped


def verify_chunks(
    run_dir: Path,
    manifest: dict[str, object],
    journal: dict[str, object],
) -> list[tuple[int, int, str]]:
    binding = PartitionBinding.from_manifest(manifest)
    transport_config = require_dict(manifest.get("transport"), "manifest.transport")
    chunks = plan_chunks(binding.partition_bytes, require_int(transport_config.get("chunk_bytes"), "chunk_bytes"))
    entries = require_dict(journal.get("chunks"), "journal.chunks")
    verified: list[tuple[int, int, str]] = []
    for index, (offset, length) in enumerate(chunks):
        digest = matching_journal_chunk(entries.get(chunk_key(index)), offset, length)
        if digest is None:
            raise BackupError(f"chunk {index} has no valid journal record")
        path = chunk_path(run_dir, index)
        if existing_chunk_digest(path, length, digest) is None:
            raise BackupError(f"chunk {index} does not match its journal record")
        verified.append((offset, length, digest))
    if set(entries) != {chunk_key(index) for index in range(len(chunks))}:
        raise BackupError("journal contains unexpected chunk records")
    return verified


def assemble_raw(
    run_dir: Path,
    manifest: dict[str, object],
    journal: dict[str, object],
) -> str:
    binding = PartitionBinding.from_manifest(manifest)
    verified = verify_chunks(run_dir, manifest, journal)
    raw_path = run_dir / f"{binding.partlabel}.raw"
    assembled = journal.get("assembled")
    if isinstance(assembled, dict):
        recorded = normalized_sha256(require_str(assembled.get("sha256"), "assembled.sha256"), "assembled.sha256")
        if raw_path.is_file() and raw_path.stat().st_size == binding.partition_bytes:
            actual = sha256_file(raw_path)
            if actual == recorded:
                atomic_write(run_dir / f"{raw_path.name}.sha256", f"{actual}  {raw_path.name}\n".encode())
                return actual
        if raw_path.exists():
            quarantine(run_dir, raw_path, "assembled image does not match its journal record")
        journal["assembled"] = None
        journal["source_verification"] = None
        write_journal(run_dir, journal)
    elif raw_path.exists():
        quarantine(run_dir, raw_path, "assembled image has no journal record")

    temporary = raw_path.with_name(f".{raw_path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    streaming_digest = hashlib.sha256()
    written = 0
    try:
        with temporary.open("xb") as output:
            for index, (_, length, digest) in enumerate(verified):
                path = chunk_path(run_dir, index)
                chunk_digest = hashlib.sha256()
                copied = 0
                with path.open("rb") as source:
                    while chunk := source.read(COPY_BYTES):
                        output.write(chunk)
                        streaming_digest.update(chunk)
                        chunk_digest.update(chunk)
                        copied += len(chunk)
                if copied != length or chunk_digest.hexdigest() != digest:
                    raise BackupError(f"chunk {index} changed during assembly")
                written += copied
            output.flush()
            os.fsync(output.fileno())
        if written != binding.partition_bytes:
            raise BackupError(f"assembled {written} bytes, expected {binding.partition_bytes}")
        os.replace(temporary, raw_path)
        fsync_directory(raw_path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    fresh_digest = sha256_file(raw_path)
    if fresh_digest != streaming_digest.hexdigest():
        raise BackupError("assembled image changed before its independent verification read")
    journal["assembled"] = {
        "path": raw_path.name,
        "bytes": str(binding.partition_bytes),
        "sha256": f"sha256:{fresh_digest}",
        "verified_at_utc": utc_now(),
    }
    journal["source_verification"] = None
    write_journal(run_dir, journal)
    atomic_write(run_dir / f"{raw_path.name}.sha256", f"{fresh_digest}  {raw_path.name}\n".encode())
    return fresh_digest


def verify_source(
    run_dir: Path,
    manifest: dict[str, object],
    journal: dict[str, object],
    transport: Transport,
    assembled_digest: str,
) -> str:
    binding = PartitionBinding.from_manifest(manifest)
    record = run_dir / ".downloads" / "source-hash.pbr.part"
    record.parent.mkdir(parents=True, exist_ok=True)
    try:
        transport.stage_hash(binding, record)
        source_digest = extract_envelope(
            record,
            None,
            binding,
            0,
            binding.partition_bytes,
            flags=FLAG_HASH_ONLY,
        )
    finally:
        with contextlib.suppress(FileNotFoundError):
            record.unlink()
    status = "matched" if source_digest == assembled_digest else "mismatch"
    journal["source_verification"] = {
        "status": status,
        "source_sha256": f"sha256:{source_digest}",
        "assembled_sha256": f"sha256:{assembled_digest}",
        "verified_at_utc": utc_now(),
    }
    write_journal(run_dir, journal)
    if status != "matched":
        raise BackupError(
            f"full source SHA-256 {source_digest} does not match assembled backup {assembled_digest}"
        )
    return source_digest


@dataclasses.dataclass(frozen=True)
class BackupResult:
    downloaded_chunks: int
    skipped_chunks: int
    raw_sha256: str
    run_dir: Path


def execute_backup(
    run_dir: Path,
    proposed_manifest: dict[str, object],
    transport: Transport,
) -> BackupResult:
    with run_lock(run_dir, exclusive=True):
        manifest, manifest_sha256, _created = prepare_manifest(run_dir, proposed_manifest)
        binding = PartitionBinding.from_manifest(manifest)
        transport.preflight(manifest, binding)
        journal, downloaded, skipped = capture_chunks(run_dir, manifest, manifest_sha256, transport)
        raw_digest = assemble_raw(run_dir, manifest, journal)
        verify_source(run_dir, manifest, journal, transport, raw_digest)
        return BackupResult(downloaded, skipped, raw_digest, run_dir)


def verify_run(run_dir: Path) -> str:
    with run_lock(run_dir, exclusive=False):
        manifest, manifest_sha256 = load_manifest(run_dir)
        journal = load_journal(run_dir, manifest_sha256)
        binding = PartitionBinding.from_manifest(manifest)
        verify_chunks(run_dir, manifest, journal)
        assembled = require_dict(journal.get("assembled"), "journal.assembled")
        recorded = normalized_sha256(require_str(assembled.get("sha256"), "assembled.sha256"), "assembled.sha256")
        raw_path = run_dir / f"{binding.partlabel}.raw"
        if not raw_path.is_file() or raw_path.stat().st_size != binding.partition_bytes:
            raise BackupError("assembled raw image is missing or has the wrong size")
        actual = sha256_file(raw_path)
        if actual != recorded:
            raise BackupError("assembled raw image does not match the journal")
        source = require_dict(journal.get("source_verification"), "journal.source_verification")
        if source.get("status") != "matched":
            raise BackupError("source verification is not matched")
        source_digest = normalized_sha256(require_str(source.get("source_sha256"), "source_sha256"), "source_sha256")
        if source_digest != actual:
            raise BackupError("source verification digest does not match the raw image")
        return actual


def positive_int(value: str) -> int:
    try:
        parsed = int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not an integer: {value}") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backup-pbread1",
        description="Make or verify a resumable, hash-bound PBREAD1 partition backup.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup", help="create or resume a backup")
    backup.add_argument("--run-dir", type=Path, required=True)
    backup.add_argument("--inventory", type=Path, required=True)
    backup.add_argument("--serial", required=True)
    backup.add_argument("--partuuid", required=True)
    backup.add_argument("--partlabel", default="userdata")
    backup.add_argument("--pocketboot-image", type=Path, required=True)
    backup.add_argument("--fastboot", default=os.environ.get("FASTBOOT", "fastboot"))
    backup.add_argument("--offline-source", type=Path, help="read this regular file instead of contacting USB")
    backup.add_argument("--chunk-bytes", type=positive_int, default=DEFAULT_CHUNK_BYTES)
    backup.add_argument(
        "--dry-run",
        action="store_true",
        help="validate and print the immutable plan without writes or USB",
    )
    verify = subparsers.add_parser("verify", help="offline verification of a completed run")
    verify.add_argument("--run-dir", type=Path, required=True)
    return parser


def command_backup(args: argparse.Namespace) -> int:
    if args.offline_source is None and args.chunk_bytes != DEFAULT_CHUNK_BYTES:
        raise BackupError(f"real-device backups require the fixed {DEFAULT_CHUNK_BYTES}-byte chunk size")
    run_dir = args.run_dir.expanduser().resolve()
    inventory_path = args.inventory.expanduser().resolve()
    pocketboot_image = args.pocketboot_image.expanduser().resolve()
    offline_source = args.offline_source.expanduser().resolve() if args.offline_source else None
    proposed = manifest_from_inputs(
        inventory_path,
        args.serial,
        args.partuuid,
        args.partlabel,
        pocketboot_image,
        args.chunk_bytes,
    )
    validate_run_manifest(proposed)
    if args.dry_run:
        print(json.dumps(proposed, indent=2, sort_keys=True))
        print("dry-run: no files written and no fastboot command executed", file=sys.stderr)
        return 0
    transport: Transport
    if offline_source is not None:
        transport = OfflineTransport(offline_source)
    else:
        transport = FastbootTransport(args.fastboot, args.serial)
    result = execute_backup(run_dir, proposed, transport)
    print(
        f"PBREAD1 backup verified: {result.run_dir} sha256:{result.raw_sha256} "
        f"({result.downloaded_chunks} captured, {result.skipped_chunks} resumed)"
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    digest = verify_run(args.run_dir.expanduser().resolve())
    print(f"PBREAD1 backup is complete and source-matched: sha256:{digest}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            return command_backup(args)
        if args.command == "verify":
            return command_verify(args)
        raise BackupError(f"unsupported command: {args.command}")
    except BackupError as error:
        print(f"backup-pbread1: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
