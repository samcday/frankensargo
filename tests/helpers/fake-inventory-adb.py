#!/usr/bin/env python3
"""Deterministic fake ADB transport for inventory-pocketboot tests."""

from __future__ import annotations

import os
import re
import struct
import sys
import uuid
import zlib


SECTOR_SIZE = int(os.environ.get("ADB_TEST_SECTOR_SIZE", "512"))
DISK_SECTORS = 4096
ENTRY_COUNT = 4
ENTRY_SIZE = 128
FIRST_USABLE = 34
LAST_USABLE = 4062
BACKUP_LBA = DISK_SECTORS - 1
BACKUP_ENTRY_LBA = BACKUP_LBA - 1
DISK_GUID = uuid.UUID(int=0)

PARTITIONS = [
    {
        "number": 1,
        "name": "system_a",
        "type_guid": uuid.UUID("97d7b011-54da-4835-b3c4-917ad6e73d74"),
        "partuuid": uuid.UUID("11111111-2222-4333-8444-555555555555"),
        "start": 34,
        "last": 133,
        "attributes": 0x004F000000000000,
    },
    {
        "number": 2,
        "name": "userdata",
        "type_guid": uuid.UUID("1b81e7e6-f50d-419b-a739-2aeef8da3335"),
        "partuuid": uuid.UUID("66666666-7777-4888-8999-aaaaaaaaaaaa"),
        "start": 134,
        "last": LAST_USABLE,
        "attributes": 0,
    },
]


def build_entries() -> bytes:
    entries = bytearray(ENTRY_COUNT * ENTRY_SIZE)
    for partition in PARTITIONS:
        offset = (partition["number"] - 1) * ENTRY_SIZE
        name = partition["name"].encode("utf-16-le")
        entry = struct.pack(
            "<16s16sQQQ72s",
            partition["type_guid"].bytes_le,
            partition["partuuid"].bytes_le,
            partition["start"],
            partition["last"],
            partition["attributes"],
            name.ljust(72, b"\x00"),
        )
        entries[offset : offset + ENTRY_SIZE] = entry
    return bytes(entries)


def build_header(current_lba: int, backup_lba: int, entry_lba: int, entries: bytes) -> bytes:
    entry_crc = zlib.crc32(entries) & 0xFFFFFFFF
    values = (
        b"EFI PART",
        0x00010000,
        92,
        0,
        0,
        current_lba,
        backup_lba,
        FIRST_USABLE,
        LAST_USABLE,
        DISK_GUID.bytes_le,
        entry_lba,
        ENTRY_COUNT,
        ENTRY_SIZE,
        entry_crc,
    )
    header = bytearray(SECTOR_SIZE)
    struct.pack_into("<8sIIIIQQQQ16sQIII", header, 0, *values)
    header_crc = zlib.crc32(header[:92]) & 0xFFFFFFFF
    struct.pack_into("<I", header, 16, header_crc)
    return bytes(header)


def build_disk() -> bytes:
    entries = build_entries()
    backup_lba = (
        1000
        if os.environ.get("ADB_TEST_UNSAFE_BACKUP_HEADER") == "1"
        else BACKUP_LBA
    )
    if os.environ.get("ADB_TEST_ALIAS_BACKUP") == "1":
        backup_entry_lba = 2
    elif os.environ.get("ADB_TEST_UNSAFE_BACKUP") == "1":
        backup_entry_lba = 100
    else:
        backup_entry_lba = BACKUP_ENTRY_LBA
    disk = bytearray(DISK_SECTORS * SECTOR_SIZE)
    primary = build_header(1, backup_lba, 2, entries)
    backup = build_header(backup_lba, 1, backup_entry_lba, entries)
    if os.environ.get("ADB_TEST_CORRUPT_PRIMARY_HEADER") == "1":
        primary = primary[:56] + bytes([primary[56] ^ 1]) + primary[57:]
    if os.environ.get("ADB_TEST_MISMATCH_BACKUP") == "1":
        changed = bytearray(backup)
        changed[56] = 1
        changed[16:20] = bytes(4)
        struct.pack_into("<I", changed, 16, zlib.crc32(changed[:92]) & 0xFFFFFFFF)
        backup = bytes(changed)
    disk[SECTOR_SIZE : 2 * SECTOR_SIZE] = primary
    disk[2 * SECTOR_SIZE : 2 * SECTOR_SIZE + len(entries)] = entries
    if backup_entry_lba != 2:
        start = backup_entry_lba * SECTOR_SIZE
        disk[start : start + len(entries)] = entries
    start = backup_lba * SECTOR_SIZE
    disk[start : start + SECTOR_SIZE] = backup
    return bytes(disk)


def write(data: bytes | str) -> None:
    if isinstance(data, str):
        data = data.encode()
    sys.stdout.buffer.write(data)


def log(arguments: list[str]) -> None:
    path = os.environ.get("ADB_TEST_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(" ".join(arguments) + "\n")


def partition_for_path(path: str) -> dict:
    match = re.search(r"mmcblk0p([0-9]+)", path)
    if not match:
        raise ValueError(path)
    number = int(match.group(1))
    return next(item for item in PARTITIONS if item["number"] == number)


def cat(paths: list[str]) -> None:
    if paths == ["/proc/device-tree/compatible"]:
        write(b"google,sargo\x00qcom,sdm670\x00")
        return
    if paths == ["/proc/device-tree/model"]:
        write(b"Synthetic Sargo\x00")
        return
    if paths == ["/sys/block/mmcblk0/device/cid"]:
        write("13014e53304a394b381011182ce76600\n")
        return
    if paths == ["/sys/class/block/mmcblk0/queue/logical_block_size"]:
        write(f"{SECTOR_SIZE}\n")
        return
    if paths == ["/sys/class/block/mmcblk0/size"]:
        write(f"{DISK_SECTORS * SECTOR_SIZE // 512}\n")
        return
    if all(path.endswith("/uevent") for path in paths):
        for path in paths:
            partition = partition_for_path(path)
            partuuid = str(partition["partuuid"])
            if os.environ.get("ADB_TEST_SYSFS_MISMATCH") == "1" and partition["number"] == 2:
                partuuid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
            write(
                "MAJOR=259\n"
                f"MINOR={partition['number']}\n"
                f"DEVNAME=mmcblk0p{partition['number']}\n"
                "DEVTYPE=partition\n"
                "DISKSEQ=9\n"
                f"PARTN={partition['number']}\n"
                f"PARTNAME={partition['name']}\n"
                f"PARTUUID={partuuid}\n"
            )
        return
    if all(path.endswith("/start") for path in paths):
        for path in paths:
            write(f"{partition_for_path(path)['start'] * SECTOR_SIZE // 512}\n")
        return
    if all(path.endswith("/size") for path in paths):
        for path in paths:
            partition = partition_for_path(path)
            sectors = partition["last"] - partition["start"] + 1
            write(f"{sectors * SECTOR_SIZE // 512}\n")
        return
    raise ValueError("unknown cat paths: " + " ".join(paths))


def main() -> int:
    arguments = sys.argv[1:]
    log(arguments)
    if len(arguments) < 3 or arguments[0] != "-s":
        return 90
    requested_serial = arguments[1]
    command = arguments[2:]
    if requested_serial != os.environ.get("ADB_TEST_EXPECTED_SERIAL", "TEST-SARGO"):
        return 91
    if command == ["get-state"]:
        write(os.environ.get("ADB_TEST_STATE", "recovery") + "\n")
        return 0
    if command == ["get-serialno"]:
        write(os.environ.get("ADB_TEST_REPORTED_SERIAL", requested_serial) + "\n")
        return 0
    if command == ["shell", "/usr/bin/id"]:
        write("uid=0 gid=0\n")
        return 0
    if command == ["shell", "/bin/uname", "-r"]:
        write("7.1.2-test\n")
        return 0
    if len(command) >= 3 and command[:2] == ["shell", "/bin/cat"]:
        try:
            cat(command[2:])
        except (StopIteration, ValueError) as error:
            print(error, file=sys.stderr)
            return 92
        return 0
    if len(command) == 4 and command[:3] == ["exec-out", "/bin/sh", "-c"]:
        match = re.fullmatch(
            r"exec /bin/dd if=/dev/mmcblk0 bs=([0-9]+) "
            r"skip=([0-9]+) count=([0-9]+) 2>/dev/null",
            command[3],
        )
        if not match:
            return 93
        block_size, skip, count = (int(value) for value in match.groups())
        disk = build_disk()
        start = block_size * skip
        end = start + block_size * count
        data = disk[start:end]
        if os.environ.get("ADB_TEST_SHORT_READ") == "1":
            data = data[:-1]
        write(data)
        return 0
    print("unhandled fake adb command: " + " ".join(command), file=sys.stderr)
    return 94


if __name__ == "__main__":
    raise SystemExit(main())
