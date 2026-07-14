# Userdata anchor bootstrap executor v1

`bin/execute-bootstrap` is the separately armed host controller for the inert
argv emitted by `target/plan-bootstrap`. It is intentionally specific to
`org.frankensargo.bootstrap-plan/1`; it does not accept a shell command, a
different action, an unvalidated command list, or a guessed intermediate LVM
layout.

Nothing in the test suite contacts a phone. The transaction engine, crash
windows, exact stage recognizer, checkpoint binding, argv substitution and
`pvcreate -ff` no-replay rule are exercised against an in-memory transport.
The production transport is ADB plus PBREAD1's narrowly scoped Fastboot source
hash operation. Every ADB file read, bounded block read, report and mutating
argv uses raw, non-PTY ADB `shell_v2`; the legacy `shell:` and `exec:` services
are never accepted as command-completion evidence.

Patch `0013-adb-shell-v2-status.patch` adds the standard five-byte inner shell
protocol frames to PocketBoot, including distinct stdout/stderr streams and a
typed one-byte exit-status frame. `lib/adb_shell_v2.py` first binds a ready
`device`/`recovery` endpoint to the exact selected serial, requires the
negotiated `shell_v2` feature, and runs a fresh random-nonce probe whose stdout
and stderr must be exact and separate. It then wraps every real argv so child
success exits with reserved shell-v2 status 173 and child failure with 174.
Legacy ADB reports host status zero regardless of its child, so it cannot
imitate a successful wrapped command; an exit-less/truncated shell-v2 stream
returns 255 and also fails closed. The status is never parsed from printable
command output. The same primitive accepts file-backed stdin for bounded image
imports without buffering the image in host memory.

## Arming contract

The executor has no implicit target. A real invocation must repeat all of:

```sh
bin/execute-bootstrap \
  --plan /durable/path/bootstrap-plan.json \
  --serial 99NAY1AZG1 \
  --partuuid db04e713-11c3-4d68-bec2-8cc483bd3891 \
  --confirm BOOTSTRAP-... \
  --recovery-attestation ABL-AND-SYSRQ-RECOVERY-PROVEN \
  --pbread-run /durable/path/pbread1-userdata \
  --state-dir /durable/path/bootstrap-state \
  --execute
```

Without the literal `--execute` flag it exits before parsing the plan, looking
up ADB/Fastboot, creating the state directory, or contacting USB. `--confirm`
must be the token inside the exact plan. The serial and PARTUUID must be
canonical and equal the plan. The fixed recovery attestation records the
operator's out-of-band ABL and UART/SysRq drill; the program cannot infer that
physical fact.

The state directory must be absolute, canonical, private to the invoking user,
and backed by a non-volatile host filesystem. `/tmp`, tmpfs, overlay and ramfs
are rejected. Its filesystem must make ordinary file and directory `fsync`
durable. Keep it with the already verified PBREAD1 backup, not inside an
ephemeral distrobox layer.

The PBREAD1 run path is also explicit and canonical. On every invocation the
executor requires the run directory and its regular, non-symlink `.lock` on a
non-volatile filesystem, then takes one shared run lock. That same lock remains
held continuously across rehashing every chunk and the complete raw image, the
live-source hash, publication of the first-write intent, and durable recording
of the first mutation outcome (in fact it remains held for the invocation).
The verifier requires the exact manifest hash, journal hash, run UUID, terminal
source-verification timestamp, raw/source SHA-256, partition binding and device
identity recorded by the plan. Once intent is written that canonical run path
is fixed for resume. A plan whose backup was modified, lost, volatile or only
partially retained cannot arm `pvcreate`.

`--stop-after-step N` is available for deliberate review pauses. Step `0`
performs the complete host-backup, live-source, identity, quiescence, runtime
and initial-LVM-state gates, persists their evidence, and executes no LVM
mutation. Steps `1` through `12` pause after that numbered durable checkpoint.
The option does not weaken any gate and a later invocation resumes from the
same state directory.

## Gates before the first write

The plan is opened without following a final symlink and must remain the same
regular file while read. The executor validates the bundled JSON schema,
recomputes `authorization_sha256`, and independently derives the confirmation
token.

Over the explicitly serial-selected PocketBoot ADB shell it then requires:

- an available ADB endpoint reporting the same serial, a negotiated and
  nonce-proven raw `shell_v2` channel, and a uid-0 shell;
- live `google,sargo` DT compatibility and the exact bound eMMC CID;
- exactly `mmcblk0p72`, numbered 72 and parented by the `mmcblk0` disk, with
  `DEVTYPE=partition`, the planned PARTUUID, `userdata` PARTNAME, start LBA,
  sector count and byte geometry; `/dev/mmcblk0p72` must be a block special
  file whose `st_rdev` exactly equals that sysfs partition's `dev` value;
- primary and backup GPT sector hashes, CRCs, zero disk GUID, aliased backup
  entry-array layout, entry-array hash, GPT type, PARTUUID, geometry and the
  `userdata` UTF-16 name prefix;
- no mountinfo record for userdata's live major:minor, no active swap, and an
  empty sysfs holders directory;
- pulled byte-for-byte `/sbin/lvm.static` and `/etc/lvm/lvm.conf` size/SHA-256
  matches, plus `lvm.static version` reporting exactly 2.03.35; and
- a fresh PBREAD1 `oem hash` envelope for the complete live userdata source,
  equal to the backup-bound SHA-256 in the plan.

The complete-source hash is required only while userdata is still exact stage
0. Its successful result and all other pre-write evidence are atomically
written and fsynced before `pvcreate`. The executor then durably binds the
exact `pvcreate` argv intent to that pre-write file before dispatch and records
the returned result or transport exception as the first-write outcome while
the PBREAD1 lock remains held. On a later legitimate resume the
partition contains LVM metadata and must no longer equal the old Android image;
the executor instead requires that durable pre-write attestation plus the exact
planned LVM state.

The device-node `st_rdev` binding and mount, swap and holder gates are repeated
immediately before every planned argv. All LVM commands and reports retain the plan's exact `--devices
/dev/mmcblk0p72`, `--nohints` and disabled implicit backup/archive fencing.

## Exact stages and crash behavior

Before a command, the host writes and fsyncs an invocation-intent JSON. After
the one remote argv returns with a complete typed shell-v2 success status, it
writes and fsyncs the result, rereads the three fenced LVM JSON reports, and
accepts only the complete next stage. Legacy status zero, missing exit frames,
transport disconnects, timeouts and nonzero children are unsuccessful dispatch
outcomes and can never satisfy a checkpoint. Reports are checked as a whole,
including:

- the frozen PV UUID, exact device, two metadata areas and allocation counts,
  plus at least the plan's complete conservative extent capacity and complete
  conservative free-extents budget after every allocation—including both the
  mandatory 16 GiB reserve and 2,306,867,200 bytes of uncommitted slack;
- one generated VG UUID, one PV, 4 MiB extents, disabled autoactivation,
  exact tags, sizes and free extent counts;
- the exact LV set, generated UUIDs, byte sizes, tags, inactivity and writable
  metadata attributes;
- every physical segment placed on the bound userdata device;
- exact `pool_tdata`, `pool_tmeta` and `lvol0_pmspare` sizes, placement, segment
  geometry, tags and LVM data/metadata/spare role attributes;
- 256 KiB thin chunks, `nopassdown`, error-when-full, and the exact thin-pool
  UUID links; and
- the unpublished, writable `disk-duranium` thin LV with only its planned
  import-pending tags.

Plan v1's `verification_argv` did not include the LVM `discards` and
`lv_when_full` report columns. The executor derives a read-only superset of
that exact fenced `lvs` argv and refuses to accept the thin-pool stage unless
both policies are observable. It does not derive or alter a mutating argv.
LVM status 5 is treated as the documented no-rows result only when the parsed
JSON contains the requested report section and that section is actually
empty. Status 5 carrying any PV, VG or LV row is an error, never stage data.

After every post-VG mutation it executes the plan's explicit read-only
`vgcfgbackup`, pulls the unique operation/step file, requires the live PV, VG
and every live LV UUID in it, hashes it, writes it to the plan-selected host
path, fsyncs the file and directory, and confirms that the LVM reports did not
change during capture. Every mutating argv and every `vgcfgbackup` must first
have a complete, trustworthy shell-v2 child status of exactly zero. A nonzero
status is journaled with the observed post-command LVM state and forces manual
forensics even if the command appears to have committed. The numbered
checkpoint contains the complete normalized LVM state, its hash, every
generated UUID, and the captured VG metadata hash.

On restart, every older checkpoint must have its complete schema body, exact
plan/operation/step binding, canonical full LVM report, matching state hash and
complete generated-ID map. PV, VG and stable LV UUIDs must remain continuous
between checkpoints. Every referenced host `vgcfgbackup` is reopened without
following a symlink, rehashed, parsed for the exact live UUID set, and matched
to its recorded size/hash/path. Each checkpoint also requires its contiguous,
durable exact command-intent and outcome history. The latest checkpoint must
hash to the exact current LVM state. If a command committed but the host died
before its checkpoint, exactly one missing latest checkpoint may be
reconstructed, and only when that exact command intent and durable outcome
already exist and the durable shell-v2 result is status zero. A disconnect,
transport exception or nonzero status cannot be promoted into a checkpoint by
observing a plausible later LVM stage. Anything between two modeled stages is
rejected.

`pvcreate --force --force` has a stricter rule: once its invocation intent is
durable, it is never issued a second time unless the first execution reached
the exact planned PV, its durable shell-v2 result is status zero, and recovery
merely needs to write the missing checkpoint. An error, cable loss, nonzero
status, or stage-0 result after an attempted
`pvcreate` is a manual-forensics stop. The executor does not decide that a
second `-ff` is harmless.

## Scope boundary

Successful step 12 means only that the userdata anchor VG and initial LV layout
exist and their final `franken.vgcfg` is durable on the host. It does not format
`ggmeta`, `boot-rescue`, `home` or `homed-state`; import or publish the Duranium
disk; rebuild the UUID-bound PocketBoot capsule; or boot Duranium. Those remain
separate, evidence-bearing transactions.

Run the offline suite with:

```sh
tests/test-bootstrap-executor.sh
```
