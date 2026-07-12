"""Deterministic, host-only userdata-anchor bootstrap planning.

This module emits destructive command argv as inert JSON.  It never executes
those commands and never opens a block device.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
import uuid
from typing import Sequence

import pbread1


PLAN_SCHEMA = "org.frankensargo.bootstrap-plan/1"
DEVICE_PLACEHOLDER = "@USERDATA_BLOCK_DEVICE@"
HOST_STATE_DIR_PLACEHOLDER = "@HOST_DURABLE_STATE_DIR@"
REMOTE_STAGING_DIR_PLACEHOLDER = "@POCKETBOOT_VOLATILE_STATE_DIR@"
LVM_STATIC = "/sbin/lvm.static"
LVM_CONFIG_OVERRIDE = "backup/backup=0 backup/archive=0"
LVM_STATIC_VERSION = "2.03.35"
LVM_STATIC_BYTES = 2_309_032
LVM_STATIC_SHA256 = (
    "b83d704df60ca281deb56f1704d74db731a05365e90d0162556b2c355b572d39"
)
LVM_CONF = "/etc/lvm/lvm.conf"
LVM_CONF_BYTES = 432
LVM_CONF_SHA256 = (
    "16eb1787836608cfaff40aa904705b2138928010b1b4011e4ab981b4d43e2998"
)
VG_NAME = "franken"
EXPECTED_PARTLABEL = "userdata"
EXPECTED_PARTTYPE = "1b81e7e6-f50d-419b-a739-2aeef8da3335"
EXPECTED_KERNEL_NAME = "mmcblk0p72"
EXPECTED_SECTOR_BYTES = 512
EXPECTED_RAW_BYTES = 53_648_801_280
PE_BYTES = 4 * 1024 * 1024
PV_METADATA_ALIGNMENT_BUDGET = 64 * 1024 * 1024
RECOVERY_RESERVE_BYTES = 16 * 1024 * 1024 * 1024
THIN_CHUNK_BYTES = 256 * 1024
DURANIUM_VIRTUAL_BYTES = 20 * 1024 * 1024 * 1024
LVM_UUID_RE = re.compile(r"^[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}$")


class PlanError(RuntimeError):
    """A bootstrap input or semantic gate failed."""


@dataclasses.dataclass(frozen=True)
class Evidence:
    inventory_schema: str
    inventory_file_sha256: str
    inventory_canonical_sha256: str
    pbread_manifest_sha256: str
    pbread_journal_sha256: str
    pbread_run_uuid: str
    pbread_source_verified_at: str
    backup_raw_sha256: str
    pocketboot_image_name: str
    pocketboot_image_bytes: int
    pocketboot_image_sha256: str
    fastboot_serial: str
    emmc_cid: str
    gpt_disk_guid: str
    gpt_entry_array_sha256: str
    gpt_primary_header_sha256: str
    gpt_backup_header_sha256: str
    partuuid: str
    parttype: str
    partlabel: str
    kernel_name: str
    start_lba: int
    sectors: int
    sector_bytes: int
    raw_bytes: int


def canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode()


def sha256_file(path: Path) -> str:
    return pbread1.sha256_file(path)


def file_identity(path: Path, field: str) -> tuple[int, int, int, int, int]:
    try:
        status = path.stat()
    except OSError as error:
        raise PlanError(f"cannot stat {field} {path}: {error}") from error
    if not stat.S_ISREG(status.st_mode):
        raise PlanError(f"{field} is not a regular file: {path}")
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def require_stable_file(
    path: Path,
    field: str,
    before: tuple[int, int, int, int, int],
) -> tuple[int, int, int, int, int]:
    after = file_identity(path, field)
    if after != before:
        raise PlanError(f"{field} changed while it was being verified")
    return after


def require_dict(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PlanError(f"{field} is not an object")
    return value


def require_list(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise PlanError(f"{field} is not an array")
    return value


def require_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise PlanError(f"{field} is not a nonempty string")
    return value


def decimal(value: object, field: str) -> int:
    try:
        return pbread1.decimal(value, field)
    except pbread1.BackupError as error:
        raise PlanError(str(error)) from error


def normalized_sha256(value: str, field: str) -> str:
    try:
        return pbread1.normalized_sha256(value, field)
    except pbread1.BackupError as error:
        raise PlanError(str(error)) from error


def normalized_uuid(value: str, field: str, *, version4: bool = False) -> str:
    try:
        parsed = uuid.UUID(value)
    except ValueError as error:
        raise PlanError(f"{field} is not a canonical UUID: {value!r}") from error
    if str(parsed) != value:
        raise PlanError(f"{field} is not a canonical lowercase UUID: {value!r}")
    if version4 and parsed.version != 4:
        raise PlanError(f"{field} must be an RFC 4122 version-4 UUID")
    return str(parsed)


def lvm_uuid(value: str) -> str:
    if not LVM_UUID_RE.fullmatch(value):
        raise PlanError("planned PV UUID is not an LVM UUID")
    if len(set(value.replace("-", ""))) < 8:
        raise PlanError("planned PV UUID does not look independently random")
    return value


def inventory_partition(inventory: dict[str, object], partuuid: str) -> dict[str, object]:
    gpt = require_dict(inventory.get("gpt"), "inventory.gpt")
    matches = []
    for candidate in require_list(gpt.get("partitions"), "inventory.gpt.partitions"):
        partition = require_dict(candidate, "inventory partition")
        if partition.get("partuuid") == partuuid:
            matches.append(partition)
    if len(matches) != 1:
        raise PlanError(f"PARTUUID must match exactly one inventory partition: {partuuid}")
    return matches[0]


def load_evidence(
    inventory_path: Path,
    pbread_run: Path,
    pocketboot_image: Path,
    serial: str,
    partuuid: str,
) -> Evidence:
    inventory_path = inventory_path.resolve()
    pbread_run = pbread_run.resolve()
    pocketboot_image = pocketboot_image.resolve()
    try:
        inventory_identity = file_identity(inventory_path, "inventory")
        inventory = pbread1.verified_inventory(inventory_path)
        inventory_file_sha256 = sha256_file(inventory_path)
        require_stable_file(inventory_path, "inventory", inventory_identity)
        # Keep the backup writer excluded across verification, parsing and the
        # journal-file hash.  Otherwise an atomic journal replacement between
        # these reads could produce a plan whose recorded hash describes a
        # different journal from the fields bound below.
        with pbread1.run_lock(pbread_run, exclusive=False):
            verified_raw_sha256 = pbread1.verify_run(pbread_run)
            pbread_manifest, pbread_manifest_sha256 = pbread1.load_manifest(pbread_run)
            journal_identity = file_identity(
                pbread_run / "journal.json", "PBREAD1 journal"
            )
            pbread_journal = pbread1.load_journal(pbread_run, pbread_manifest_sha256)
            pbread_journal_sha256 = sha256_file(pbread_run / "journal.json")
            require_stable_file(
                pbread_run / "journal.json", "PBREAD1 journal", journal_identity
            )
    except (OSError, pbread1.BackupError) as error:
        raise PlanError(str(error)) from error

    pocketboot_identity = file_identity(pocketboot_image, "PocketBoot image")
    if not pbread1.SERIAL_RE.fullmatch(serial) or serial.startswith("-"):
        raise PlanError("serial contains unsafe characters")
    partuuid = normalized_uuid(partuuid, "requested PARTUUID")

    device = require_dict(inventory.get("device"), "inventory.device")
    emmc = require_dict(device.get("emmc"), "inventory.device.emmc")
    gpt = require_dict(inventory.get("gpt"), "inventory.gpt")
    partition = inventory_partition(inventory, partuuid)
    if device.get("product") != "sargo" or "google,sargo" not in require_list(
        device.get("compatible"), "inventory.device.compatible"
    ):
        raise PlanError("inventory is not for google,sargo")
    if device.get("adb_serial") != serial:
        raise PlanError("explicit serial does not match the inventory")
    if gpt.get("disk_guid") != "00000000-0000-0000-0000-000000000000":
        raise PlanError("inventory does not preserve frankensargo's zero GPT disk GUID")
    if gpt.get("disk_guid_is_zero") is not True:
        raise PlanError("inventory zero-GUID flag is inconsistent")
    if gpt.get("backup_entry_array_layout") != "aliases-primary":
        raise PlanError("inventory backup GPT layout is not aliases-primary")

    pb_device = require_dict(pbread_manifest.get("device"), "PBREAD1 manifest.device")
    pb_inventory = require_dict(
        pbread_manifest.get("inventory"), "PBREAD1 manifest.inventory"
    )
    pb_partition = require_dict(
        pbread_manifest.get("partition"), "PBREAD1 manifest.partition"
    )
    pb_pocketboot = require_dict(
        pbread_manifest.get("pocketboot"), "PBREAD1 manifest.pocketboot"
    )
    source = require_dict(
        pbread_journal.get("source_verification"),
        "PBREAD1 journal.source_verification",
    )
    assembled = require_dict(
        pbread_journal.get("assembled"), "PBREAD1 journal.assembled"
    )
    if source.get("status") != "matched":
        raise PlanError("PBREAD1 source verification is not matched")
    source_sha256 = normalized_sha256(
        require_str(source.get("source_sha256"), "source_sha256"),
        "source_sha256",
    )
    assembled_sha256 = normalized_sha256(
        require_str(assembled.get("sha256"), "assembled.sha256"),
        "assembled.sha256",
    )
    if source_sha256 != assembled_sha256 or source_sha256 != verified_raw_sha256:
        raise PlanError("PBREAD1 full source and destination hashes disagree")

    inventory_canonical_sha256 = normalized_sha256(
        require_str(inventory.get("canonical_sha256"), "inventory canonical SHA-256"),
        "inventory canonical SHA-256",
    )
    entry_array_sha256 = normalized_sha256(
        require_str(gpt.get("entry_array_sha256"), "GPT entry-array SHA-256"),
        "GPT entry-array SHA-256",
    )
    if pb_device.get("fastboot_serial") != serial:
        raise PlanError("PBREAD1 manifest serial does not match")
    if pb_device.get("emmc_cid") != emmc.get("cid"):
        raise PlanError("PBREAD1 manifest CID does not match")
    if pb_device.get("gpt_disk_guid") != gpt.get("disk_guid"):
        raise PlanError("PBREAD1 manifest GPT disk GUID does not match")
    if normalized_sha256(
        require_str(pb_inventory.get("canonical_sha256"), "PBREAD1 inventory SHA-256"),
        "PBREAD1 inventory SHA-256",
    ) != inventory_canonical_sha256:
        raise PlanError("PBREAD1 manifest inventory hash does not match")
    if normalized_sha256(
        require_str(pb_inventory.get("entry_array_sha256"), "PBREAD1 GPT SHA-256"),
        "PBREAD1 GPT SHA-256",
    ) != entry_array_sha256:
        raise PlanError("PBREAD1 manifest GPT entry-array hash does not match")

    expected_partition_fields = {
        "partuuid": partition.get("partuuid"),
        "type_guid": partition.get("type_guid"),
        "partlabel": partition.get("name"),
        "kernel_name_observation": partition.get("kernel_node_observation"),
        "start_lba": partition.get("start_lba"),
        "sectors": partition.get("sector_count"),
        "logical_sector_bytes": emmc.get("logical_sector_size"),
        "raw_bytes": partition.get("byte_size"),
    }
    for field, expected in expected_partition_fields.items():
        if pb_partition.get(field) != expected:
            raise PlanError(f"PBREAD1 partition {field} does not match inventory")

    try:
        image_sha256 = sha256_file(pocketboot_image)
        require_stable_file(pocketboot_image, "PocketBoot image", pocketboot_identity)
    except OSError as error:
        raise PlanError(f"cannot hash PocketBoot image {pocketboot_image}: {error}") from error
    pb_image_sha256 = normalized_sha256(
        require_str(pb_pocketboot.get("image_sha256"), "PocketBoot image SHA-256"),
        "PocketBoot image SHA-256",
    )
    if image_sha256 != pb_image_sha256:
        raise PlanError("supplied PocketBoot image does not match the PBREAD1 run")
    image_bytes = pocketboot_identity[2]
    if decimal(pb_pocketboot.get("image_bytes"), "PocketBoot image bytes") != image_bytes:
        raise PlanError("supplied PocketBoot image size does not match the PBREAD1 run")

    primary_header = require_dict(gpt.get("primary_header"), "GPT primary header")
    backup_header = require_dict(gpt.get("backup_header"), "GPT backup header")
    return Evidence(
        inventory_schema=require_str(inventory.get("schema"), "inventory schema"),
        inventory_file_sha256=inventory_file_sha256,
        inventory_canonical_sha256=inventory_canonical_sha256,
        pbread_manifest_sha256=pbread_manifest_sha256,
        pbread_journal_sha256=pbread_journal_sha256,
        pbread_run_uuid=normalized_uuid(
            require_str(pbread_manifest.get("run_uuid"), "PBREAD1 run UUID"),
            "PBREAD1 run UUID",
        ),
        pbread_source_verified_at=require_str(
            source.get("verified_at_utc"), "source verification timestamp"
        ),
        backup_raw_sha256=source_sha256,
        pocketboot_image_name=pocketboot_image.name,
        pocketboot_image_bytes=image_bytes,
        pocketboot_image_sha256=image_sha256,
        fastboot_serial=serial,
        emmc_cid=require_str(emmc.get("cid"), "eMMC CID"),
        gpt_disk_guid=require_str(gpt.get("disk_guid"), "GPT disk GUID"),
        gpt_entry_array_sha256=entry_array_sha256,
        gpt_primary_header_sha256=normalized_sha256(
            require_str(primary_header.get("sector_sha256"), "primary GPT SHA-256"),
            "primary GPT SHA-256",
        ),
        gpt_backup_header_sha256=normalized_sha256(
            require_str(backup_header.get("sector_sha256"), "backup GPT SHA-256"),
            "backup GPT SHA-256",
        ),
        partuuid=partuuid,
        parttype=normalized_uuid(
            require_str(partition.get("type_guid"), "partition type GUID"),
            "partition type GUID",
        ),
        partlabel=require_str(partition.get("name"), "partition label"),
        kernel_name=require_str(
            partition.get("kernel_node_observation"), "kernel observation"
        ),
        start_lba=decimal(partition.get("start_lba"), "partition start LBA"),
        sectors=decimal(partition.get("sector_count"), "partition sectors"),
        sector_bytes=emmc.get("logical_sector_size"),
        raw_bytes=decimal(partition.get("byte_size"), "partition bytes"),
    )


def volume_layout(partuuid: str) -> list[dict[str, object]]:
    anchor = {"partuuid": partuuid, "policy": "exact-anchor-pv-only"}
    return [
        {
            "name": "ggmeta",
            "role": "transaction-metadata",
            "kind": "thick",
            "size_bytes": str(512 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": ["greygoo.critical", "pocketboot.meta.v1"],
        },
        {
            "name": "boot-rescue",
            "role": "rescue-boot-filesystem",
            "kind": "thick",
            "size_bytes": str(2 * 1024 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": ["greygoo.critical", "pocketboot.bootfs.v1"],
        },
        {
            "name": "home",
            "role": "shared-homed-backing-store",
            "kind": "thick",
            "size_bytes": str(8 * 1024 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": ["frankensargo.home.v1", "greygoo.critical"],
        },
        {
            "name": "homed-state",
            "role": "shared-homed-records-and-keys",
            "kind": "thick",
            "size_bytes": str(256 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": ["frankensargo.homed-state.v1", "greygoo.critical"],
        },
        {
            "name": "pool-meta",
            "role": "thin-pool-metadata",
            "kind": "thin-metadata",
            "size_bytes": str(512 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": ["greygoo.critical", "greygoo.thin-metadata.v1"],
        },
        {
            "name": "lvol0_pmspare",
            "role": "lvm-managed-thin-metadata-spare",
            "kind": "thin-metadata-spare",
            "size_bytes": str(512 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": True,
            "allocation": anchor,
            "tags": [],
        },
        {
            "name": "pool",
            "role": "thin-pool-data",
            "kind": "thin-data",
            "size_bytes": str(20 * 1024 * 1024 * 1024),
            "virtual_bytes": None,
            "critical": False,
            "allocation": anchor,
            "tags": ["greygoo.replaceable", "greygoo.thin-pool.v1"],
        },
        {
            "name": "disk-duranium",
            "role": "duranium-whole-disk",
            "kind": "thin",
            "size_bytes": "0",
            "virtual_bytes": str(DURANIUM_VIRTUAL_BYTES),
            "critical": False,
            "allocation": {"thin_pool": "pool", "policy": "thin-only"},
            "tags": [
                "distro.duranium",
                "greygoo.import-pending",
                "greygoo.replaceable",
            ],
        },
    ]


def lvm_argv(applet: str, *arguments: str) -> list[str]:
    """Return PocketBoot's actual static multicall invocation with scan fencing."""
    return [
        LVM_STATIC,
        applet,
        "--devices",
        DEVICE_PLACEHOLDER,
        "--nohints",
        "--config",
        LVM_CONFIG_OVERRIDE,
        *arguments,
    ]


def lvcreate_argv(
    name: str,
    size: str,
    tags: list[str],
    *,
    contiguous: bool = True,
) -> list[str]:
    arguments = [
        "--yes",
        "--activate",
        "n",
        "--permission",
        "rw",
        "--wipesignatures",
        "n",
        "--zero",
        "n",
    ]
    if contiguous:
        arguments.extend(["--contiguous", "y"])
    arguments.extend(["--size", size, "--name", name])
    for tag in tags:
        arguments.extend(["--addtag", tag])
    arguments.extend([VG_NAME, DEVICE_PLACEHOLDER])
    return lvm_argv("lvcreate", *arguments)


def command_plan(planned_pv_uuid: str) -> list[dict[str, object]]:
    commands: list[dict[str, object]] = []

    def append_command(
        step: str,
        destructive: bool,
        argv: list[str],
        *,
        expected_before: list[str],
        expected_after: list[str],
        capture_fields: list[str],
    ) -> None:
        ordinal = len(commands) + 1
        state_stem = f"{ordinal:02d}-{step}"
        vgcfgbackup_argv: list[str] | None = None
        remote_capture: dict[str, object] | None = None
        if ordinal > 1 and destructive:
            remote_path = f"{REMOTE_STAGING_DIR_PLACEHOLDER}/steps/{state_stem}.vgcfg"
            host_path = f"{HOST_STATE_DIR_PLACEHOLDER}/vgcfg/{state_stem}.vgcfg"
            vgcfgbackup_argv = lvm_argv(
                "vgcfgbackup", "--readonly", "--file", remote_path, VG_NAME
            )
            remote_capture = {
                "source": remote_path,
                "host_destination": host_path,
                "hash": "sha256",
                "pull_hash_and_fsync_before_next_step": True,
            }
        elif step == "backup-vg-metadata":
            remote_path = f"{REMOTE_STAGING_DIR_PLACEHOLDER}/franken.vgcfg"
            remote_capture = {
                "source": remote_path,
                "host_destination": f"{HOST_STATE_DIR_PLACEHOLDER}/franken.vgcfg",
                "hash": "sha256",
                "pull_hash_and_fsync_before_next_step": True,
            }
        commands.append(
            {
                "step": step,
                "destructive": destructive,
                "argv": argv,
                "checkpoint": {
                    "ordinal": ordinal,
                    "state_file": (
                        f"{HOST_STATE_DIR_PLACEHOLDER}/steps/{state_stem}.json"
                    ),
                    "resume_policy": (
                        "accept-exact-postcondition-else-require-exact-precondition"
                    ),
                    "expected_before": expected_before,
                    "expected_after": expected_after,
                    "capture_fields": capture_fields,
                    "vgcfgbackup_argv": vgcfgbackup_argv,
                    "remote_capture": remote_capture,
                    "fsync_before_next_step": True,
                },
            }
        )

    append_command(
        "create-anchor-pv",
        True,
        lvm_argv(
            "pvcreate",
                "--yes",
                "--force",
                "--force",
                "--uuid",
                planned_pv_uuid,
                "--norestorefile",
                "--zero",
                "y",
                "--dataalignment",
                "1m",
                "--metadatasize",
                "16m",
                "--pvmetadatacopies",
                "2",
                DEVICE_PLACEHOLDER,
        ),
        expected_before=[
            "bound-partition-has-no-lvm-signature",
            "franken-vg-is-absent",
        ],
        expected_after=["bound-partition-is-exact-planned-pv"],
        capture_fields=["pv_uuid", "pv_name", "pv_size", "pv_free", "pv_tags"],
    )
    append_command(
        "create-anchor-vg",
        True,
        lvm_argv(
            "vgcreate",
                "--yes",
                "--physicalextentsize",
                "4m",
                "--setautoactivation",
                "n",
                "--addtag",
                "pocketboot.vg.v1",
                VG_NAME,
                DEVICE_PLACEHOLDER,
        ),
        expected_before=["planned-pv-exists-unassigned", "franken-vg-is-absent"],
        expected_after=["franken-vg-exists-on-exact-planned-pv"],
        capture_fields=["pv_uuid", "vg_uuid", "vg_name", "vg_extent_size", "vg_tags"],
    )
    append_command(
        "tag-anchor-pv",
        True,
        lvm_argv(
            "pvchange",
                "--yes",
                "--addtag",
                "greygoo.anchor",
                "--addtag",
                "pocketboot.pv.v1",
                DEVICE_PLACEHOLDER,
        ),
        expected_before=["franken-vg-and-planned-pv-match-checkpoints"],
        expected_after=["planned-pv-has-exact-anchor-tags"],
        capture_fields=["pv_uuid", "pv_name", "pv_tags", "vg_uuid"],
    )
    thick = [
        ("ggmeta", "512m", ["greygoo.critical", "pocketboot.meta.v1"]),
        ("boot-rescue", "2g", ["greygoo.critical", "pocketboot.bootfs.v1"]),
        ("home", "8g", ["frankensargo.home.v1", "greygoo.critical"]),
        (
            "homed-state",
            "256m",
            ["frankensargo.homed-state.v1", "greygoo.critical"],
        ),
        ("pool-meta", "512m", ["greygoo.critical", "greygoo.thin-metadata.v1"]),
        ("pool", "20g", ["greygoo.replaceable", "greygoo.thin-pool.v1"]),
    ]
    for name, size, tags in thick:
        append_command(
            f"create-{name}",
            True,
            lvcreate_argv(name, size, tags),
            expected_before=[
                "franken-vg-and-planned-pv-match-checkpoints",
                f"{name}-lv-is-absent",
            ],
            expected_after=[f"{name}-lv-has-exact-size-tags-and-anchor-placement"],
            capture_fields=[
                "lv_uuid",
                "lv_name",
                "lv_size",
                "lv_tags",
                "lv_attr",
                "segtype",
                "devices",
                "vg_uuid",
            ],
        )

    append_command(
        "convert-thin-pool",
        True,
        lvm_argv(
            "lvconvert",
                    "--yes",
                    "--type",
                    "thin-pool",
                    "--chunksize",
                    "256k",
                    "--poolmetadata",
                    f"{VG_NAME}/pool-meta",
                    "--poolmetadataspare",
                    "y",
                    "--discards",
                    "nopassdown",
                    "--errorwhenfull",
                    "y",
                    f"{VG_NAME}/pool",
                    DEVICE_PLACEHOLDER,
        ),
        expected_before=[
            "pool-and-pool-meta-match-checkpoints",
            "thin-pool-and-pmspare-are-absent",
        ],
        expected_after=[
            "pool-is-exact-thin-pool",
            "pool-tmeta-and-lvol0-pmspare-are-anchor-pinned",
        ],
        capture_fields=[
            "pool_lv_uuid",
            "pool_tdata_lv_uuid",
            "pool_tmeta_lv_uuid",
            "pmspare_lv_uuid",
            "lv_size",
            "lv_tags",
            "lv_attr",
            "segtype",
            "devices",
            "vg_uuid",
        ],
    )
    append_command(
        "create-duranium-thin-disk",
        True,
        lvm_argv(
            "lvcreate",
                    "--yes",
                    "--activate",
                    "n",
                    "--permission",
                    "rw",
                    "--zero",
                    "n",
                    "--type",
                    "thin",
                    "--thinpool",
                    "pool",
                    "--virtualsize",
                    "20g",
                    "--name",
                    "disk-duranium",
                    "--addtag",
                    "distro.duranium",
                    "--addtag",
                    "greygoo.import-pending",
                    "--addtag",
                    "greygoo.replaceable",
                    VG_NAME,
        ),
        expected_before=[
            "pool-thin-layout-matches-checkpoint",
            "disk-duranium-lv-is-absent",
        ],
        expected_after=["disk-duranium-is-exact-import-pending-thin-lv"],
        capture_fields=[
            "lv_uuid",
            "lv_name",
            "lv_size",
            "lv_tags",
            "lv_attr",
            "segtype",
            "pool_lv_uuid",
            "vg_uuid",
        ],
    )
    append_command(
        "backup-vg-metadata",
        False,
        lvm_argv(
            "vgcfgbackup",
                    "--readonly",
                    "--file",
                    f"{REMOTE_STAGING_DIR_PLACEHOLDER}/franken.vgcfg",
                    VG_NAME,
        ),
        expected_before=["complete-lvm-layout-matches-all-checkpoints"],
        expected_after=["durable-vgcfgbackup-matches-live-vg-metadata"],
        capture_fields=["vg_uuid", "vgcfgbackup_sha256"],
    )
    return commands


def build_plan(
    evidence: Evidence,
    operation_uuid: str,
    planned_pv_uuid: str,
) -> dict[str, object]:
    operation_uuid = normalized_uuid(operation_uuid, "operation UUID", version4=True)
    planned_pv_uuid = lvm_uuid(planned_pv_uuid)
    if evidence.partlabel != EXPECTED_PARTLABEL:
        raise PlanError("bootstrap target is not the userdata partition")
    if evidence.parttype != EXPECTED_PARTTYPE:
        raise PlanError("userdata GPT type does not match the inventoried Android userdata type")
    if evidence.kernel_name != EXPECTED_KERNEL_NAME:
        raise PlanError("userdata is not the fixed mmcblk0p72 bootstrap target")
    if evidence.sector_bytes != EXPECTED_SECTOR_BYTES:
        raise PlanError("userdata logical sector size is not 512 bytes")
    if evidence.raw_bytes != evidence.sectors * evidence.sector_bytes:
        raise PlanError("userdata geometry does not equal its raw byte count")
    if evidence.raw_bytes != EXPECTED_RAW_BYTES:
        raise PlanError(
            f"userdata size changed: expected {EXPECTED_RAW_BYTES}, got {evidence.raw_bytes}"
        )
    if evidence.gpt_disk_guid != "00000000-0000-0000-0000-000000000000":
        raise PlanError("device GPT disk GUID is not the observed zero GUID")

    volumes = volume_layout(evidence.partuuid)
    physical_allocated = sum(int(volume["size_bytes"]) for volume in volumes)
    extent_capacity = (
        (evidence.raw_bytes - PV_METADATA_ALIGNMENT_BUDGET) // PE_BYTES
    ) * PE_BYTES
    extent_rounding_tail = (
        evidence.raw_bytes - PV_METADATA_ALIGNMENT_BUDGET - extent_capacity
    )
    planned_free = extent_capacity - physical_allocated
    slack = planned_free - RECOVERY_RESERVE_BYTES
    if physical_allocated % PE_BYTES != 0:
        raise PlanError("a planned physical allocation is not PE-aligned")
    if slack <= 0:
        raise PlanError("layout does not leave the mandatory recovery reserve")

    core: dict[str, object] = {
        "schema": PLAN_SCHEMA,
        "action": "bootstrap-userdata-anchor",
        "planner_effect": "read-files-and-emit-json-only",
        "operation_uuid": operation_uuid,
        "artifacts": {
            "inventory": {
                "schema": evidence.inventory_schema,
                "file_sha256": f"sha256:{evidence.inventory_file_sha256}",
                "canonical_sha256": f"sha256:{evidence.inventory_canonical_sha256}",
            },
            "pbread1": {
                "run_uuid": evidence.pbread_run_uuid,
                "manifest_sha256": f"sha256:{evidence.pbread_manifest_sha256}",
                "journal_sha256": f"sha256:{evidence.pbread_journal_sha256}",
                "source_verified_at_utc": evidence.pbread_source_verified_at,
                "status": "source-matched",
                "raw_sha256": f"sha256:{evidence.backup_raw_sha256}",
            },
            "pocketboot": {
                "image_name": evidence.pocketboot_image_name,
                "image_bytes": str(evidence.pocketboot_image_bytes),
                "image_sha256": f"sha256:{evidence.pocketboot_image_sha256}",
            },
        },
        "device": {
            "product": "sargo",
            "compatible": "google,sargo",
            "fastboot_serial": evidence.fastboot_serial,
            "emmc_cid": evidence.emmc_cid,
            "gpt_disk_guid": evidence.gpt_disk_guid,
            "gpt_entry_array_sha256": f"sha256:{evidence.gpt_entry_array_sha256}",
            "gpt_primary_header_sha256": f"sha256:{evidence.gpt_primary_header_sha256}",
            "gpt_backup_header_sha256": f"sha256:{evidence.gpt_backup_header_sha256}",
        },
        "partition": {
            "partuuid": evidence.partuuid,
            "type_guid": evidence.parttype,
            "partlabel": evidence.partlabel,
            "kernel_name_observation": evidence.kernel_name,
            "start_lba": str(evidence.start_lba),
            "sectors": str(evidence.sectors),
            "logical_sector_bytes": evidence.sector_bytes,
            "raw_bytes": str(evidence.raw_bytes),
            "current_full_source_sha256": f"sha256:{evidence.backup_raw_sha256}",
        },
        "lvm": {
            "vg_name": VG_NAME,
            "physical_extent_bytes": str(PE_BYTES),
            "pv": {
                "planned_uuid": planned_pv_uuid,
                "uuid_policy": "operator-generated-random-and-frozen-before-first-write",
                "metadata_copies": 2,
                "metadata_area_bytes_each": str(16 * 1024 * 1024),
                "data_alignment_bytes": str(1024 * 1024),
                "tags": ["greygoo.anchor", "pocketboot.pv.v1"],
            },
            "vg_uuid_policy": "lvm-generated-capture-and-fsync-before-next-mutation",
            "lv_uuid_policy": "lvm-generated-capture-and-fsync-after-each-create",
            "vg_tags": ["pocketboot.vg.v1"],
            "thin_pool": {
                "name": "pool",
                "chunk_bytes": str(THIN_CHUNK_BYTES),
                "error_when_full": True,
                "discard_policy": "nopassdown",
                "automatic_metadata_spare": True,
                "managed_spare_lv": "lvol0_pmspare",
            },
            "volumes": volumes,
            "capacity": {
                "partition_bytes": str(evidence.raw_bytes),
                "pv_metadata_alignment_budget_bytes": str(
                    PV_METADATA_ALIGNMENT_BUDGET
                ),
                "conservative_extent_capacity_after_budget_bytes": str(
                    extent_capacity
                ),
                "extent_rounding_tail_bytes": str(extent_rounding_tail),
                "planned_physical_lv_bytes": str(physical_allocated),
                "planned_free_extents_after_allocations_bytes": str(planned_free),
                "mandatory_recovery_reserve_bytes": str(RECOVERY_RESERVE_BYTES),
                "uncommitted_slack_beyond_reserve_bytes": str(slack),
                "duranium_virtual_bytes": str(DURANIUM_VIRTUAL_BYTES),
            },
        },
        "transaction": {
            "lvm_binary": LVM_STATIC,
            "runtime_artifacts": {
                "lvm_static": {
                    "path": LVM_STATIC,
                    "version": LVM_STATIC_VERSION,
                    "bytes": str(LVM_STATIC_BYTES),
                    "sha256": f"sha256:{LVM_STATIC_SHA256}",
                },
                "lvm_conf": {
                    "path": LVM_CONF,
                    "bytes": str(LVM_CONF_BYTES),
                    "sha256": f"sha256:{LVM_CONF_SHA256}",
                },
            },
            "device_placeholder": DEVICE_PLACEHOLDER,
            "host_state_directory_placeholder": HOST_STATE_DIR_PLACEHOLDER,
            "pocketboot_volatile_state_directory_placeholder": (
                REMOTE_STAGING_DIR_PLACEHOLDER
            ),
            "execution_policy": (
                "one-command-then-verify-and-fsync-checkpoint-never-blind-replay"
            ),
            "recovery_gate": "manual-out-of-band-operator-attestation-required",
            "runtime_attestation_policy": (
                "pull-and-match-exact-runtime-files-and-lvm-version-before-first-write"
            ),
            "preconditions": [
                "resolve-device-placeholder-by-exact-sysfs-partuuid-scan-and-recheck-all-bound-identity",
                "rehash-complete-live-source-with-pbread1-oem-hash-and-match-current-full-source-sha256",
                "pull-and-hash-runtime-files-and-match-lvm-version-before-first-write",
                "persist-and-fsync-bootstrap-intent-before-first-command",
                "record-manual-stock-fastboot-and-sysrq-recovery-attestation-before-first-command",
                "require-explicit-confirmation-token-in-a-separate-executor",
            ],
            "command_argv": command_plan(planned_pv_uuid),
            "post_import_argv": [
                {
                    "step": "publish-verified-duranium-disk",
                    "argv": lvm_argv(
                        "lvchange",
                        "--permission",
                        "r",
                        "--deltag",
                        "greygoo.import-pending",
                        "--addtag",
                        "pocketboot.disk.v1",
                        f"{VG_NAME}/disk-duranium",
                    ),
                    "checkpoint": {
                        "ordinal": 13,
                        "state_file": (
                            f"{HOST_STATE_DIR_PLACEHOLDER}/steps/"
                            "13-publish-verified-duranium-disk.json"
                        ),
                        "resume_policy": (
                            "accept-exact-postcondition-else-require-exact-precondition"
                        ),
                        "expected_before": [
                            "disk-duranium-import-is-complete-and-readback-hash-verified",
                            "disk-duranium-is-exact-import-pending-thin-lv",
                        ],
                        "expected_after": [
                            "disk-duranium-is-read-only-published-pocketboot-disk"
                        ],
                        "capture_fields": [
                            "lv_uuid",
                            "lv_name",
                            "lv_tags",
                            "lv_attr",
                            "pool_lv_uuid",
                            "vg_uuid",
                        ],
                        "vgcfgbackup_argv": lvm_argv(
                            "vgcfgbackup",
                            "--readonly",
                            "--file",
                            (
                                f"{REMOTE_STAGING_DIR_PLACEHOLDER}/steps/"
                                "13-publish-verified-duranium-disk.vgcfg"
                            ),
                            VG_NAME,
                        ),
                        "remote_capture": {
                            "source": (
                                f"{REMOTE_STAGING_DIR_PLACEHOLDER}/steps/"
                                "13-publish-verified-duranium-disk.vgcfg"
                            ),
                            "host_destination": (
                                f"{HOST_STATE_DIR_PLACEHOLDER}/vgcfg/"
                                "13-publish-verified-duranium-disk.vgcfg"
                            ),
                            "hash": "sha256",
                            "pull_hash_and_fsync_before_next_step": True,
                        },
                        "fsync_before_next_step": True,
                    },
                }
            ],
            "verification_argv": [
                lvm_argv(
                    "pvs",
                    "--readonly",
                    "--nolocking",
                    "--reportformat",
                    "json_std",
                    "--units",
                    "b",
                    "--nosuffix",
                    "-o",
                    (
                        "pv_uuid,pv_name,dev_size,pv_size,pv_free,pe_start,"
                        "pv_mda_size,pv_mda_free,pv_mda_count,pv_mda_used_count,"
                        "pv_pe_count,pv_pe_alloc_count,pv_tags,vg_uuid,vg_name"
                    ),
                    DEVICE_PLACEHOLDER,
                ),
                lvm_argv(
                    "vgs",
                    "--readonly",
                    "--nolocking",
                    "--reportformat",
                    "json_std",
                    "-o",
                    (
                        "vg_uuid,vg_name,vg_size,vg_free,vg_extent_size,"
                        "vg_extent_count,vg_free_count,pv_count,lv_count,"
                        "vg_missing_pv_count,vg_mda_count,vg_mda_used_count,"
                        "vg_autoactivation,vg_tags"
                    ),
                    VG_NAME,
                ),
                lvm_argv(
                    "lvs",
                    "--readonly",
                    "--nolocking",
                    "--reportformat",
                    "json_std",
                    "--units",
                    "b",
                    "--nosuffix",
                    "--segments",
                    "-a",
                    "-o",
                    (
                        "vg_uuid,lv_uuid,lv_name,lv_size,lv_active,lv_permissions,"
                        "segtype,seg_start_pe,seg_size_pe,devices,metadata_devices,"
                        "data_lv_uuid,metadata_lv_uuid,pool_lv_uuid,lv_tags,lv_attr,"
                        "chunk_size"
                    ),
                    VG_NAME,
                ),
            ],
            "placement_assertions": {
                "allowed_physical_partuuids": [evidence.partuuid],
                "all_physical_extents_userdata_only": True,
                "critical_extents_userdata_pinned": True,
                "thin_metadata_and_spare_userdata_pinned": True,
                "automatic_allocation_from_future_pvs": False,
            },
        },
    }
    digest = hashlib.sha256(canonical_json_bytes(core)).hexdigest()
    token = f"BOOTSTRAP-{operation_uuid.split('-', 1)[0]}-{digest[:12]}"
    return {
        **core,
        "authorization_sha256": f"sha256:{digest}",
        "confirmation": {
            "required_by_future_executor": True,
            "token": token,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plan-bootstrap",
        description=(
            "Validate completed userdata backup evidence and emit an inert, "
            "deterministic LVM bootstrap plan."
        ),
    )
    parser.add_argument("--inventory", required=True, type=Path)
    parser.add_argument("--pbread-run", required=True, type=Path)
    parser.add_argument("--pocketboot-image", required=True, type=Path)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--partuuid", required=True)
    parser.add_argument("--operation-uuid", required=True)
    parser.add_argument("--planned-pv-uuid", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        evidence = load_evidence(
            args.inventory,
            args.pbread_run,
            args.pocketboot_image,
            args.serial,
            args.partuuid,
        )
        plan = build_plan(evidence, args.operation_uuid, args.planned_pv_uuid)
    except PlanError as error:
        print(f"plan-bootstrap: {error}", file=sys.stderr)
        return 1
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
