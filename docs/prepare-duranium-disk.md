# Derive the Duranium disk image

`bin/prepare-duranium-disk` makes a new whole-disk raw image whose nested ESP
can boot the published Duranium UKI with the frankensargo outer-LVM adapter.
It runs as the ordinary `deck` user inside `fedora-latest`: it does not use a
loop device, mount a filesystem, require root, or contact the phone.

The source is never modified. Both the output raw and provenance paths must be
absent. The tool first verifies the published artifact, decompressed raw, UKI,
embedded base-profile command line, and adapter hashes. It independently
validates both GPT headers and arrays (including CRCs), the complete partition
geometry, the one exact ESP, and its FAT32 bounds. It then uses mtools against
the byte offset of that ESP and proves that every byte outside the ESP stayed
unchanged.

## Fedora prerequisites

The tested environment is the Deck's `fedora-latest` distrobox with Python 3,
`mtools`, and `zstd` installed. Check it without privilege:

```sh
distrobox enter --name fedora-latest -- sh -lc \
  'python3 --version; mcopy --version | head -1; zstd --version'
```

If `mcopy` or `mdir` is absent, install the Fedora `mtools` package rather than
falling back to a mounted or loop-backed mutation. The tool deliberately fails
with that instruction. mtools runs with sanity checks enabled, no user config,
UTC, and a fixed FAT-era `SOURCE_DATE_EPOCH`; repeated raw and zstd derivations
from the same bytes therefore produce the same output SHA-256.

## Pinned 26070701 invocation

Build the adapter first with `bin/build-duranium-lvm-adapter`. Then run the
derivation from the Fedora distrobox. The adapter SHA below is calculated from
that already-built file; every published identity and geometry value is fixed:

```sh
cd /home/deck/src/frankensargo

ARTIFACTS=/home/deck/frankensargo-lab/artifacts/duranium
ADAPTER="$ARTIFACTS/frankensargo-duranium-lvm-adapter.cpio"
ADAPTER_SHA=$(sha256sum "$ADAPTER" | awk '{print $1}')

bin/prepare-duranium-disk \
  --source "$ARTIFACTS/google-sargo_phosh_edge_26070701.raw.zst" \
  --source-format zst \
  --source-sha256 035fa4b4f1ea70f6d2706f7e0d60e4c3f97d36f7571de739fbb29eab02f99f68 \
  --raw-sha256 1e911b82a87325a6c3a5624cbbcd8c157d2c4a3abca6dd568b6c49727eb00e34 \
  --disk-bytes 6862950400 \
  --disk-guid eb5e7a01-f599-4065-b5c8-4f715b6a6d39 \
  --esp-partuuid 120c6e48-0d10-4817-94d2-31dd39e8a4cf \
  --esp-start-lba 2048 \
  --esp-sectors 2097152 \
  --uki "$ARTIFACTS/google-sargo_phosh_edge_26070701.efi" \
  --uki-sha256 eedefda43cb97ced8d4be0b6a50c1354cbb840ac798c3d795dabb6972e213757 \
  --cmdline-sha256 8b2fceff7c861d907e75b763edacde3009b5a7df14fe3dddf17e9e5a8578e355 \
  --adapter "$ADAPTER" \
  --adapter-sha256 "$ADAPTER_SHA" \
  --output "$ARTIFACTS/google-sargo_phosh_edge_26070701.frankensargo.raw" \
  --provenance "$ARTIFACTS/google-sargo_phosh_edge_26070701.frankensargo.json"
```

For an already decompressed source, use `--source-format raw`, point `--source`
at the `.raw`, and set `--source-sha256` to the same value as
`--raw-sha256`. The result is byte-identical.

## Exact ESP changes

The published UKI already at
`/EFI/Linux/google-sargo_phosh_edge_26070701.efi` must byte-match the separately
pinned UKI. The tool refuses to replace it. It adds or replaces only these
files inside the exact ESP:

- `/EFI/Linux/frankensargo-duranium-lvm-26070701.cpio` — the pinned adapter;
- `/loader/entries/frankensargo-duranium.conf` — a Type #1 PocketBoot BLS
  `uki` entry selecting profile 0 and appending the adapter; and
- `/loader/loader.conf` — the published comments followed by
  `default frankensargo-duranium.conf`.

The BLS `options` value preserves the complete embedded base-profile Duranium
command line and appends exactly:

```text
root=dissect mount.usr=dissect sysrq_always_enabled=1
```

The last token keeps downstream Magic SysRq available after kexec. Conflicting
pre-existing `root=` or `mount.usr=` values are rejected rather than silently
overridden.

The JSON provenance includes the exact BLS and loader contents, every injected
file hash, the original loader hash, complete GPT/ESP geometry, FAT geometry,
the outside-ESP hash, and the final whole-disk hash. The tool also extracts all
four relevant ESP files through mtools after mutation and byte-compares them
before publishing the output.

## Boot-selection gate

The derived image encodes the standard BLS `loader.conf` default. PocketBoot's
patch stack can load a BLS `uki` plus external initrd and patch 0007 consumes
`loader/loader.conf`; its parser, stem/filename match, and preference ordering
are covered by offline unit tests. The remaining gate is an end-to-end boot of
this derived disk proving that the BLS entry wins over duplicate direct Type #2
discovery on frankensargo hardware. Provenance states that narrower unproved
boundary. A synthetic Fedora `grubenv` is intentionally not injected as a
workaround.
