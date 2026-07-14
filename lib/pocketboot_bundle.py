#!/usr/bin/env python3
"""Crash-detectable publication and verification for PocketBoot build bundles."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import NoReturn

from pocketboot_profile import ProfileError, validate_metadata


FORMAT = "org.frankensargo.pocketboot-build-bundle/1"
PROFILES = frozenset(("lab", "lab-no-acm"))
SAFE_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024


class BundleError(RuntimeError):
    pass


class SimulatedHardCrash(BaseException):
    """Test-only analogue of SIGKILL: intentionally bypasses rollback."""


def fail(message: str) -> NoReturn:
    raise BundleError(message)


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def canonical_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def safe_basename(value: str, field: str) -> str:
    if not SAFE_BASENAME.fullmatch(value) or value in (".", ".."):
        fail(f"{field} is not a safe basename")
    return value


def bundle_names(image_name: str, profile: str) -> dict[str, str]:
    image_name = safe_basename(image_name, "image name")
    if profile not in PROFILES:
        fail(f"unsupported PocketBoot bundle profile: {profile}")
    result = {
        "image": image_name,
        "sha256": f"{image_name}.sha256",
        "provenance": f"{image_name}.provenance",
        "manifest": f"{image_name}.bundle.json",
    }
    if profile == "lab-no-acm":
        result["profile"] = f"{image_name}.profile.json"
    return result


@dataclasses.dataclass(frozen=True)
class HeldMember:
    role: str
    path: Path
    basename: str
    size: int
    mode: int
    sha256: str
    data: bytes | None

    def manifest_record(self) -> dict[str, object]:
        return {
            "role": self.role,
            "basename": self.basename,
            "bytes": self.size,
            "mode": f"{self.mode:04o}",
            "sha256": self.sha256,
        }


def hold_member(
    role: str,
    path: Path,
    basename: str,
    maximum: int,
    *,
    retain_data: bool,
) -> HeldMember:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError as error:
        fail(f"cannot open temporary {role}: {error}")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            fail(f"temporary {role} is not a nonempty bounded regular file")
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                fail(f"temporary {role} ended while hashing")
            digest.update(block)
            if retain_data:
                chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            fail(f"temporary {role} grew while hashing")
        os.fsync(descriptor)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IMODE(value.st_mode),
        )
        if identity(before) != identity(after):
            fail(f"temporary {role} changed while hashing")
        return HeldMember(
            role=role,
            path=path,
            basename=safe_basename(basename, f"{role} basename"),
            size=before.st_size,
            mode=stat.S_IMODE(before.st_mode),
            sha256=digest.hexdigest(),
            data=b"".join(chunks) if retain_data else None,
        )
    finally:
        os.close(descriptor)


def parse_provenance(data: bytes) -> list[str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        fail("PocketBoot provenance is not ASCII")
    if not text.endswith("\n"):
        fail("PocketBoot provenance lacks a final newline")
    lines = text.splitlines()
    if not lines or any(not line or "=" not in line for line in lines):
        fail("PocketBoot provenance is not canonical key/value text")
    return lines


def one_provenance_value(lines: list[str], key: str) -> str:
    prefix = f"{key}="
    values = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
    if len(values) != 1 or not values[0]:
        fail(f"PocketBoot provenance lacks exactly one {key}")
    return values[0]


def validate_relationships(profile: str, members: dict[str, HeldMember]) -> None:
    expected_roles = {"image", "sha256", "provenance"}
    if profile == "lab-no-acm":
        expected_roles.add("profile")
    if set(members) != expected_roles:
        fail("PocketBoot bundle member roles differ from its profile")
    image = members["image"]
    checksum = members["sha256"]
    provenance = members["provenance"]
    assert checksum.data is not None and provenance.data is not None
    expected_checksum = f"{image.sha256}  {image.basename}\n".encode("ascii")
    if checksum.data != expected_checksum:
        fail("PocketBoot checksum sidecar does not exactly bind the image")
    lines = parse_provenance(provenance.data)
    if one_provenance_value(lines, "profile") != profile:
        fail("PocketBoot provenance profile differs from the bundle")
    if one_provenance_value(lines, "sha256") != image.sha256:
        fail("PocketBoot provenance hash differs from the image")
    if profile == "lab-no-acm":
        profiled = members["profile"]
        assert profiled.data is not None
        try:
            value = json.loads(profiled.data)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            fail(f"PocketBoot profile metadata is invalid JSON: {error}")
        try:
            value = validate_metadata(value)
        except ProfileError as error:
            fail(f"PocketBoot profile metadata contract is invalid: {error}")
        if value.get("result_sha256") != image.sha256 or value.get("bytes") != image.size:
            fail("PocketBoot profile metadata differs from the image")
        metadata_line = one_provenance_value(lines, "profile_metadata")
        expected = f"{profiled.basename} sha256={profiled.sha256}"
        if metadata_line != expected:
            fail("PocketBoot provenance does not bind the profile metadata")
        provenance_contract = {
            "profile_application": value["application"],
            "profile_source_delta": "none",
            "profile_base_cmdline": value["base_cmdline"],
            "profile_effective_cmdline": value["effective_cmdline"],
            "profile_parent_sha256": value["parent_sha256"],
            "profile_result_sha256": value["result_sha256"],
            "profile_allowed_ranges": json.dumps(
                value["allowed_ranges"], separators=(",", ":")
            ),
            "profile_changed_spans": json.dumps(
                value["changed_spans"], separators=(",", ":")
            ),
            "profile_all_other_bytes_unchanged": "true",
            "profile_unsigned_unsealed_assertion": value[
                "unsigned_unsealed_assertion"
            ],
        }
        for key, expected_value in provenance_contract.items():
            if one_provenance_value(lines, key) != expected_value:
                fail(f"PocketBoot provenance {key} differs from profile metadata")


def manifest_value(profile: str, image_name: str, members: dict[str, HeldMember]) -> dict[str, object]:
    names = bundle_names(image_name, profile)
    return {
        "format": FORMAT,
        "profile": profile,
        "image": names["image"],
        "published_last": names["manifest"],
        "members": [members[role].manifest_record() for role in sorted(members)],
    }


def check_destination(output_dir: Path, image_name: str, profile: str) -> dict[str, str]:
    names = bundle_names(image_name, profile)
    if not output_dir.is_dir():
        fail(f"PocketBoot output directory does not exist: {output_dir}")
    for basename in names.values():
        path = output_dir / basename
        if os.path.lexists(path):
            fail(f"refusing existing PocketBoot bundle path: {path}")
    return names


def publish_bundle(
    *,
    output_dir: Path,
    image_name: str,
    profile: str,
    temporary_members: dict[str, Path],
    _simulate_hard_crash_after_members: int | None = None,
) -> Path:
    output_dir = output_dir.resolve()
    names = check_destination(output_dir, image_name, profile)
    expected_roles = set(names) - {"manifest"}
    if set(temporary_members) != expected_roles:
        fail("temporary PocketBoot bundle roles differ from the requested profile")
    members: dict[str, HeldMember] = {}
    for role in sorted(expected_roles):
        temporary = Path(os.path.abspath(temporary_members[role]))
        if temporary.parent.resolve() != output_dir:
            fail(f"temporary {role} is outside the PocketBoot output directory")
        members[role] = hold_member(
            role,
            temporary,
            names[role],
            MAX_IMAGE_BYTES if role == "image" else MAX_SIDECAR_BYTES,
            retain_data=role != "image",
        )
    validate_relationships(profile, members)
    manifest = manifest_value(profile, image_name, members)
    manifest_data = json.dumps(manifest, indent=2, sort_keys=True).encode() + b"\n"
    if len(manifest_data) > MAX_MANIFEST_BYTES:
        fail("PocketBoot bundle manifest exceeds its size limit")
    descriptor, manifest_temporary_name = tempfile.mkstemp(
        prefix=f".{image_name}.", suffix=".bundle.tmp", dir=output_dir
    )
    manifest_temporary = Path(manifest_temporary_name)
    published: list[Path] = []
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(manifest_data)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = -1
        for count, role in enumerate(sorted(members), 1):
            destination = output_dir / names[role]
            os.link(members[role].path, destination, follow_symlinks=False)
            published.append(destination)
            if _simulate_hard_crash_after_members == count:
                raise SimulatedHardCrash(f"simulated hard crash after {count} members")
        # Every member link is durable before the completion manifest can exist.
        fsync_directory(output_dir)
        manifest_path = output_dir / names["manifest"]
        os.link(manifest_temporary, manifest_path, follow_symlinks=False)
        published.append(manifest_path)
        fsync_directory(output_dir)
        for member in members.values():
            member.path.unlink()
        manifest_temporary.unlink()
        fsync_directory(output_dir)
        verify_bundle(manifest_path)
        return manifest_path
    except Exception:
        for path in reversed(published):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        fsync_directory(output_dir)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            manifest_temporary.unlink()
        except FileNotFoundError:
            pass


def read_manifest(path: Path) -> dict[str, object]:
    held = hold_member(
        "manifest", path, path.name, MAX_MANIFEST_BYTES, retain_data=True
    )
    assert held.data is not None
    try:
        value = json.loads(held.data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        fail(f"PocketBoot bundle manifest is invalid JSON: {error}")
    if not isinstance(value, dict):
        fail("PocketBoot bundle manifest is not an object")
    return value


def verify_bundle(manifest_path: Path) -> dict[str, object]:
    # Canonicalize the spelling without following the final component: the
    # O_NOFOLLOW read below must reject a symlinked completion authority.
    manifest_path = Path(os.path.abspath(manifest_path))
    value = read_manifest(manifest_path)
    if set(value) != {"format", "profile", "image", "published_last", "members"}:
        fail("PocketBoot bundle manifest has unexpected fields")
    profile = value.get("profile")
    image_name = value.get("image")
    if not isinstance(profile, str) or not isinstance(image_name, str):
        fail("PocketBoot bundle manifest profile/image is malformed")
    names = bundle_names(image_name, profile)
    if (
        value.get("format") != FORMAT
        or value.get("published_last") != names["manifest"]
        or manifest_path.name != names["manifest"]
    ):
        fail("PocketBoot bundle manifest identity is invalid")
    raw_members = value.get("members")
    if not isinstance(raw_members, list):
        fail("PocketBoot bundle manifest members are not an array")
    expected_roles = set(names) - {"manifest"}
    records: dict[str, dict[str, object]] = {}
    for raw in raw_members:
        if not isinstance(raw, dict) or set(raw) != {
            "role",
            "basename",
            "bytes",
            "mode",
            "sha256",
        }:
            fail("PocketBoot bundle manifest member is malformed")
        role = raw.get("role")
        if not isinstance(role, str) or role in records:
            fail("PocketBoot bundle manifest repeats or malforms a role")
        records[role] = raw
    if set(records) != expected_roles:
        fail("PocketBoot bundle manifest member roles differ from its profile")
    held_members: dict[str, HeldMember] = {}
    for role in sorted(expected_roles):
        raw = records[role]
        basename = raw.get("basename")
        if basename != names[role]:
            fail(f"PocketBoot bundle {role} basename is invalid")
        path = manifest_path.parent / names[role]
        held = hold_member(
            role,
            path,
            names[role],
            MAX_IMAGE_BYTES if role == "image" else MAX_SIDECAR_BYTES,
            retain_data=role != "image",
        )
        if (
            raw.get("bytes") != held.size
            or raw.get("mode") != f"{held.mode:04o}"
            or raw.get("sha256") != held.sha256
        ):
            fail(f"PocketBoot bundle {role} differs from the completion manifest")
        held_members[role] = held
    validate_relationships(profile, held_members)
    return value


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check-destination")
    publish = subparsers.add_parser("publish")
    for command in (check, publish):
        command.add_argument("--output-dir", required=True, type=Path)
        command.add_argument("--image-name", required=True)
        command.add_argument("--profile", required=True, choices=sorted(PROFILES))
    publish.add_argument("--image-temp", required=True, type=Path)
    publish.add_argument("--sha256-temp", required=True, type=Path)
    publish.add_argument("--provenance-temp", required=True, type=Path)
    publish.add_argument("--profile-temp", type=Path)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", required=True, type=Path)
    return result


def main() -> int:
    try:
        args = parser().parse_args()
        if args.command == "check-destination":
            names = check_destination(args.output_dir.resolve(), args.image_name, args.profile)
            print(args.output_dir.resolve() / names["manifest"])
        elif args.command == "publish":
            temporary = {
                "image": args.image_temp,
                "sha256": args.sha256_temp,
                "provenance": args.provenance_temp,
            }
            if args.profile_temp is not None:
                temporary["profile"] = args.profile_temp
            manifest = publish_bundle(
                output_dir=args.output_dir,
                image_name=args.image_name,
                profile=args.profile,
                temporary_members=temporary,
            )
            print(manifest)
        else:
            value = verify_bundle(args.manifest)
            print(json.dumps(value, sort_keys=True))
        return 0
    except (BundleError, OSError) as error:
        print(f"pocketboot-bundle: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
