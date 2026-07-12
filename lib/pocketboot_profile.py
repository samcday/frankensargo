#!/usr/bin/env python3
"""Fail-closed PocketBoot build-profile and Android-v2 cmdline helpers."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import tomllib


ACM_TOKEN = "pocketboot.acm"
SYSRQ_TOKEN = "sysrq_always_enabled=1"
PROFILE = "lab-no-acm"
METADATA_FORMAT = "org.frankensargo.pocketboot-image-profile/1"
MAX_CONFIG_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 128 * 1024 * 1024
ANDROID_MAGIC = b"ANDROID!"
ANDROID_CMDLINE_OFFSET = 64
ANDROID_CMDLINE_BYTES = 512
ANDROID_EXTRA_CMDLINE_OFFSET = 608
ANDROID_EXTRA_CMDLINE_BYTES = 1024
EXPECTED_HEADER_VERSION = 2
AVB_FOOTER_MAGIC = b"AVBf"
CMDLINE_RANGES = (
    (ANDROID_CMDLINE_OFFSET, ANDROID_CMDLINE_OFFSET + ANDROID_CMDLINE_BYTES),
    (
        ANDROID_EXTRA_CMDLINE_OFFSET,
        ANDROID_EXTRA_CMDLINE_OFFSET + ANDROID_EXTRA_CMDLINE_BYTES,
    ),
)


class ProfileError(RuntimeError):
    pass


def fail(message: str) -> "None":
    raise ProfileError(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_cmdline(value: str, field: str) -> str:
    if not value or value != value.strip() or "  " in value:
        fail(f"{field} has noncanonical whitespace")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        fail(f"{field} is not ASCII")
    if any(byte < 0x20 or byte > 0x7E for byte in encoded):
        fail(f"{field} contains a non-printable byte")
    if len(encoded) >= ANDROID_CMDLINE_BYTES + ANDROID_EXTRA_CMDLINE_BYTES:
        fail(f"{field} does not fit the Android v2 cmdline fields")
    return value


def without_acm_cmdline(base: str) -> str:
    base = canonical_cmdline(base, "base PocketBoot cmdline")
    tokens = base.split(" ")
    if tokens.count(ACM_TOKEN) != 1:
        fail(f"base PocketBoot cmdline must contain exactly one {ACM_TOKEN}")
    if any(token.startswith(ACM_TOKEN) and token != ACM_TOKEN for token in tokens):
        fail("base PocketBoot cmdline contains an ambiguous ACM parameter")
    if tokens.count(SYSRQ_TOKEN) != 1:
        fail(f"base PocketBoot cmdline must contain exactly one {SYSRQ_TOKEN}")
    effective = " ".join(token for token in tokens if token != ACM_TOKEN)
    return canonical_cmdline(effective, "no-ACM PocketBoot cmdline")


def config_cmdlines(data: bytes) -> tuple[str, str]:
    if len(data) > MAX_CONFIG_BYTES:
        fail("Sargo device configuration exceeds the size limit")
    try:
        text = data.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        fail(f"Sargo device configuration is invalid TOML: {error}")
    if "cmdline" in parsed:
        fail("Sargo device configuration has a forbidden top-level cmdline")
    bootimg = parsed.get("bootimg")
    if not isinstance(bootimg, dict) or not isinstance(bootimg.get("cmdline"), str):
        fail("Sargo device configuration lacks [bootimg].cmdline")
    base = canonical_cmdline(bootimg["cmdline"], "base PocketBoot cmdline")
    canonical_line = f"cmdline = {json.dumps(base)}"
    if text.splitlines().count(canonical_line) != 1:
        fail("[bootimg].cmdline is not one canonical TOML line")
    return base, without_acm_cmdline(base)


def android_boot_cmdline(data: bytes) -> str:
    minimum = ANDROID_EXTRA_CMDLINE_OFFSET + ANDROID_EXTRA_CMDLINE_BYTES
    if len(data) < minimum or data[:8] != ANDROID_MAGIC:
        fail("artifact is not an Android boot image")
    header_version = int.from_bytes(data[40:44], "little")
    if header_version != EXPECTED_HEADER_VERSION:
        fail(
            f"Android boot image header version is {header_version}, "
            f"expected {EXPECTED_HEADER_VERSION}"
        )
    raw = b"".join(data[start:end] for start, end in CMDLINE_RANGES)
    text, separator, padding = raw.partition(b"\0")
    if not separator or any(padding):
        fail("Android boot image cmdline has noncanonical padding")
    try:
        decoded = text.decode("ascii")
    except UnicodeDecodeError:
        fail("Android boot image cmdline is not ASCII")
    return canonical_cmdline(decoded, "Android boot image cmdline")


def encoded_android_cmdline(value: str) -> bytes:
    value = canonical_cmdline(value, "effective Android boot cmdline")
    encoded = value.encode("ascii") + b"\0"
    capacity = ANDROID_CMDLINE_BYTES + ANDROID_EXTRA_CMDLINE_BYTES
    return encoded + b"\0" * (capacity - len(encoded))


def changed_spans(before: bytes, after: bytes) -> list[list[int]]:
    if len(before) != len(after):
        fail("profile rewrite changed the artifact length")
    result: list[list[int]] = []
    start: int | None = None
    for offset, (left, right) in enumerate(zip(before, after, strict=True)):
        if left != right and start is None:
            start = offset
        elif left == right and start is not None:
            result.append([start, offset])
            start = None
    if start is not None:
        result.append([start, len(before)])
    return result


def rewrite_android_boot_no_acm(data: bytes) -> tuple[bytes, dict[str, object]]:
    if len(data) >= 64 and data[-64:-60] == AVB_FOOTER_MAGIC:
        fail("refusing to post-process an AVB-sealed boot image")
    base = android_boot_cmdline(data)
    effective = without_acm_cmdline(base)
    packed = encoded_android_cmdline(effective)
    result = bytearray(data)
    first = packed[:ANDROID_CMDLINE_BYTES]
    second = packed[ANDROID_CMDLINE_BYTES:]
    result[CMDLINE_RANGES[0][0] : CMDLINE_RANGES[0][1]] = first
    result[CMDLINE_RANGES[1][0] : CMDLINE_RANGES[1][1]] = second
    rendered = bytes(result)
    spans = changed_spans(data, rendered)
    if not spans:
        fail("no-ACM profile did not change the Android boot image")
    for start, end in spans:
        if not any(start >= allowed_start and end <= allowed_end for allowed_start, allowed_end in CMDLINE_RANGES):
            fail("no-ACM profile changed a byte outside the Android cmdline fields")
    if android_boot_cmdline(rendered) != effective:
        fail("no-ACM profile did not produce the exact effective cmdline")
    metadata: dict[str, object] = {
        "format": METADATA_FORMAT,
        "profile": PROFILE,
        "application": "android-v2-header-postprocess",
        "base_cmdline": base,
        "effective_cmdline": effective,
        "parent_sha256": sha256_bytes(data),
        "result_sha256": sha256_bytes(rendered),
        "bytes": len(data),
        "allowed_ranges": [list(value) for value in CMDLINE_RANGES],
        "changed_spans": spans,
        "all_other_bytes_unchanged": True,
        "unsigned_unsealed_assertion": "android-v2-without-avb-footer",
    }
    return rendered, metadata


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_replace(path: Path, data: bytes, mode: int) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def read_regular(path: Path, maximum: int, field: str) -> tuple[bytes, int]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        fail(f"cannot open {field} {path}: {error}")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
            fail(f"{field} is not a bounded regular file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                fail(f"{field} ended while being read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            fail(f"{field} grew while being read")
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            fail(f"{field} changed while being read")
        return b"".join(chunks), stat.S_IMODE(before.st_mode)
    finally:
        os.close(descriptor)


def write_metadata(path: Path, value: dict[str, object]) -> None:
    validate_metadata(value)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    atomic_replace(path, data, 0o600)


def validate_metadata(value: object) -> dict[str, object]:
    expected = {
        "format",
        "profile",
        "application",
        "base_cmdline",
        "effective_cmdline",
        "parent_sha256",
        "result_sha256",
        "bytes",
        "allowed_ranges",
        "changed_spans",
        "all_other_bytes_unchanged",
        "unsigned_unsealed_assertion",
    }
    if not isinstance(value, dict) or set(value) != expected:
        fail("profile metadata has an unexpected shape")
    if (
        value.get("format") != METADATA_FORMAT
        or value.get("profile") != PROFILE
        or value.get("application") != "android-v2-header-postprocess"
        or value.get("all_other_bytes_unchanged") is not True
        or value.get("unsigned_unsealed_assertion")
        != "android-v2-without-avb-footer"
    ):
        fail("profile metadata contract fields differ")
    base = value.get("base_cmdline")
    effective = value.get("effective_cmdline")
    if not isinstance(base, str) or not isinstance(effective, str):
        fail("profile metadata cmdlines are malformed")
    if without_acm_cmdline(base) != effective:
        fail("profile metadata cmdline transition is invalid")
    for field in ("parent_sha256", "result_sha256"):
        digest = value.get(field)
        if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            fail(f"profile metadata {field} is invalid")
    if value["parent_sha256"] == value["result_sha256"]:
        fail("profile metadata parent and result hashes are equal")
    if not isinstance(value.get("bytes"), int) or value["bytes"] <= 0:
        fail("profile metadata byte size is invalid")
    if value.get("allowed_ranges") != [list(item) for item in CMDLINE_RANGES]:
        fail("profile metadata allowed ranges differ")
    spans = value.get("changed_spans")
    if not isinstance(spans, list) or not spans:
        fail("profile metadata has no changed spans")
    for span in spans:
        if (
            not isinstance(span, list)
            or len(span) != 2
            or not all(isinstance(item, int) for item in span)
            or span[0] >= span[1]
            or not any(
                span[0] >= start and span[1] <= end for start, end in CMDLINE_RANGES
            )
        ):
            fail("profile metadata changed span is invalid")
    return value


def cli() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    config = subparsers.add_parser("config-cmdline")
    config.add_argument("--config", type=Path, required=True)
    config.add_argument("--effective", action="store_true")
    inspect = subparsers.add_parser("image-cmdline")
    inspect.add_argument("--image", type=Path, required=True)
    rewrite = subparsers.add_parser("rewrite-image-no-acm")
    rewrite.add_argument("--image", type=Path, required=True)
    rewrite.add_argument("--metadata", type=Path, required=True)
    metadata = subparsers.add_parser("metadata-field")
    metadata.add_argument("--metadata", type=Path, required=True)
    metadata.add_argument(
        "--field",
        choices=(
            "base_cmdline",
            "effective_cmdline",
            "parent_sha256",
            "result_sha256",
            "allowed_ranges",
            "changed_spans",
            "all_other_bytes_unchanged",
            "unsigned_unsealed_assertion",
        ),
        required=True,
    )
    return parser


def main() -> int:
    try:
        args = cli().parse_args()
        if args.command == "config-cmdline":
            data, _mode = read_regular(args.config, MAX_CONFIG_BYTES, "device config")
            base, effective = config_cmdlines(data)
            print(effective if args.effective else base)
        elif args.command == "image-cmdline":
            data, _mode = read_regular(args.image, MAX_IMAGE_BYTES, "boot image")
            print(android_boot_cmdline(data))
        elif args.command == "rewrite-image-no-acm":
            data, mode = read_regular(args.image, MAX_IMAGE_BYTES, "boot image")
            rendered, metadata = rewrite_android_boot_no_acm(data)
            atomic_replace(args.image, rendered, mode)
            observed, _mode = read_regular(args.image, MAX_IMAGE_BYTES, "rewritten boot image")
            if sha256_bytes(observed) != metadata["result_sha256"]:
                fail("rewritten boot image differs after publication")
            write_metadata(args.metadata, metadata)
            print(metadata["parent_sha256"])
        else:
            data, _mode = read_regular(args.metadata, 64 * 1024, "profile metadata")
            try:
                value = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                fail(f"profile metadata is invalid JSON: {error}")
            value = validate_metadata(value)
            observed = value.get(args.field)
            if isinstance(observed, str):
                print(observed)
            else:
                print(json.dumps(observed, separators=(",", ":"), sort_keys=True))
        return 0
    except (OSError, ProfileError) as error:
        print(f"pocketboot-profile: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
