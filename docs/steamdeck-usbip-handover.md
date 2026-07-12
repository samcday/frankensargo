# Steam Deck remote-lab handover

This records the 2026-07-11 USB/IP experiment and the preferred 2026-07-12
Deck-local path from `sam-desktop` to frankensargo through `steamdeck`.
`userdata` was never mounted or written. The only persistent phone-side changes
in the direct-cable work were three explicit `set_active a` operations across
the recovery sessions, which reset A/B boot-control metadata; no boot image or
partition payload was flashed.

## Topology and identities

- Steam Deck tailnet address during the run: `100.64.0.8`.
- Steam Deck kernel: `6.11.11-valve27-1-neptune-611-g60ef8556a811`.
- Frankensargo PocketBoot gadget during the USB/IP run: Deck bus ID `3-1.4`,
  USB `1d6b:0104`, serial `99NAY1AZG1`, five interfaces (ACM, fastboot, ADB,
  and mass storage). Direct Deck attachment used physical port `3-1.1`. The
  first cable produced a sustained reset storm there; its replacement gave a
  clean exact-serial enumeration.
- Frankensargo's proven FTDI UART: Deck bus ID `3-1.3.4.3.1`, normally
  `ttyUSB1`, USB `0403:6001`. Another FT232 is at `3-1.3.4.3.4`; both clone
  serial `A50285BI`, so `/dev/serial/by-id` collides and is not identity.
- Desktop kernel/client during the run: Fedora kernel `7.0.12-201.fc44.x86_64`
  with `vhci_hcd` and usbip-utils `5.7.9`.

The Deck gained a native USB/IP tree under `/opt/usbip`; its daemon ran as
`/opt/usbip/usr/bin/usbipd --ipv4`. A validated sudoers drop-in was installed
as `/etc/sudoers.d/zz-deck-nopasswd`, owner `root:root`, mode `0440`, to make
the `deck` account passwordless. That is a persistent security-policy change,
not a project requirement; remove it when no longer wanted and validate the
remaining policy with `visudo -cf /etc/sudoers`.

Temporary tool experiments may remain under `/tmp/frankensargo-usbip` and
`/tmp/frankensargo-usbip-native`. The latter may hold roughly 78 MiB of
download-only package cache because a temporary pacman database treated the
Deck as empty. Nothing from that cache was installed; `/tmp` cleanup or a Deck
reboot removes it. A copied fastboot/image pair may also remain under
`/tmp/frankensargo-pocketboot`; the verified image and a temporary official
fastboot binary were staged under
`/home/deck/.local/share/frankensargo-lab/` for distrobox access.

USB/IP has no transport authentication. The observed daemon listened on
`0.0.0.0:3240`. Tailnet encryption protects traffic addressed to the Tailscale
IP but does not by itself stop LAN clients reaching another listening address.
A durable setup must firewall TCP/3240 to `tailscale0` or put the listener
behind an equivalent constrained tunnel.

## What worked

Both exports reached desktop VHCI. The imported PocketBoot descriptor retained
all five interfaces and the exact serial. These controls passed:

```sh
adb -s 99NAY1AZG1 get-state
fastboot -s 99NAY1AZG1 getvar product
tio /dev/ttyACM0
```

ADB reported `recovery`, fastboot reported `pocketboot`, and `/dev/ttyACM0`
plus `/dev/ttyUSB0` appeared. One complete
`bin/inventory-pocketboot --serial 99NAY1AZG1` run succeeded over USB/IP. It
validated all 72 GPT entries against sysfs and emitted canonical hash
`sha256:45fd308cf74558665e1b33ff4e5d488c88634afcb5240fc7a99e7df24fbd3ade`.

## Failure signature

A repeat inventory stopped at the root-shell identity read with `error:
closed`. Subsequent `lsusb`, exact-serial fastboot, ADB, and `usbip detach`
calls all blocked. The desktop process table showed:

- the ADB server in `usb_kill_urb`;
- `lsusb` and fastboot in `usbdev_open`;
- `usbip detach` in `bConfigurationValue_show`;
- several USB/SCSI recovery workers in uninterruptible D state; and
- two established kernel-owned TCP/3240 sockets, one per export.

The Deck then disappeared from the tailnet before a server-side unbind could
complete. `ss -K` could not destroy the kernel-owned sockets. Once the sockets
eventually closed, the desktop kernel unwound without a reboot and `usbip
port` returned an empty import set.

This does not reproduce the earlier `dev-sargo` bulk-OUT failure: the USB/IP
path carried a full GPT inventory successfully first. The precise trigger is
not proven. The strongest suspects are the PocketBoot mass-storage interface's
eight zero-media SCSI LUNs, concurrent export of the FTDI UART, and abrupt loss
of the server while URBs were outstanding.

## Preferred Deck-local path

On 2026-07-12, a `systemd-run --user` socat capture survived roughly eleven
hours and recorded a complete stock reboot. ABL's eMMC serial `11182ce7`
matches the known CID, proving the UART-to-phone binding. Slot A then failed
with `Invalid boot magic` and `BootPrepareAsync Volume Corrupt` and entered
bootloader mode. Stock fastboot captured this pre-repair state:

```text
current-slot=a
slot-count=2
slot-suffixes=_a,_b
has-slot:boot=yes
slot-unbootable:a=yes  slot-successful:a=no  slot-retry-count:a=0
slot-unbootable:b=no   slot-successful:b=no  slot-retry-count:b=3
```

ABL refused a transient boot while active slot A was unbootable. The one
boot-metadata write in this session was deliberately explicit:

```sh
fastboot -s 99NAY1AZG1 set_active a
```

It left `current-slot=a`, cleared `slot-unbootable:a`, and restored
`slot-retry-count:a=3`. The following transient boot succeeded; its kernel
command line reported `androidboot.slot_retry_count=2`, consistent with ABL
consuming one attempt. No `flash` or `erase` command was issued, and neither
boot partition was rewritten.

A replacement cable exposed stock fastboot as `18d1:4ee0`, exact serial
`99NAY1AZG1`. `fedora-latest` is a Fedora 44 distrobox and could access the USB
device unprivileged. The actual transient boot used official fastboot
`37.0.0-14910828`, SHA-256
`76dde33fee8b1fd00bcaf2e7f94ddef6407f0beb5bc3a98a3d4127307af23f3a`,
from the shared staging directory. It downloaded the verified 7,831,552-byte
PocketBoot image and booted it without `flash` or `erase`; the preceding
same-slot retry reset was the only boot-control mutation in that run. Two
later same-slot resets are recorded in the later control section below.

Fedora's packaged `android-tools-35.0.2-17.fc44` is the preferred repeatable
interface. Use the named-container form so noninteractive sessions do not
depend on Distrobox's shorthand parsing. A future transient boot can stay
entirely inside the distrobox after verifying the shared image:

```sh
distrobox enter --name fedora-latest -- bash -lc '
  sha256sum /home/deck/.local/share/frankensargo-lab/pocketboot-sargo-lab.img
  /usr/bin/fastboot -s 99NAY1AZG1 getvar product
  /usr/bin/fastboot -s 99NAY1AZG1 boot \
    /home/deck/.local/share/frankensargo-lab/pocketboot-sargo-lab.img
'
```

The required image digest is
`98983cc3331de0f08d6a578b89f87f2b5003607e30cb7ae5d218eb56612d48a6`.
After the boot, these checks passed from inside the same distrobox:

```sh
distrobox enter --name fedora-latest -- bash -lc '
  /usr/bin/fastboot -s 99NAY1AZG1 getvar product
  /usr/bin/adb -s 99NAY1AZG1 get-state
  /usr/bin/adb -s 99NAY1AZG1 shell /usr/bin/id
  /usr/bin/adb -s 99NAY1AZG1 shell /bin/cat \
    /sys/block/mmcblk0/device/cid
'
```

They returned product `pocketboot`, ADB state `recovery`, `uid=0 gid=0`, and
CID `13014e53304a394b381011182ce76600`. `socat-1.8.1.1-1.fc44` was installed
in the distrobox and also opened the physical UART successfully. Prefer its
stable path over the mutable tty number:

```text
/dev/serial/by-path/pci-0000:04:00.3-platform-xhci-hcd.2.auto-usb-0:1.3.4.3.1:1.0-port0
```

With no other reader holding that path, enter the root shell from the
distrobox with:

```sh
distrobox enter --name fedora-latest -- bash -lc '
  uart=$(readlink -f \
    /dev/serial/by-path/pci-0000:04:00.3-platform-xhci-hcd.2.auto-usb-0:1.3.4.3.1:1.0-port0)
  exec /usr/bin/socat -,rawer,echo=0 "$uart",b115200,rawer,echo=0
'
```

Resolving the stable symlink first is important: the literal by-path name
contains colons, which `socat` otherwise interprets as address syntax.

The UART journal showed both `tty0` and `ttyMSM0` getty supervisors at 115200
and a BusyBox 1.38.0 prompt. Interactive read-only commands then proved:

```text
uid=0 gid=0
fd 0=/dev/ttyMSM0
shell=/bin/sh --
kernel=7.1.2+
model=Google Pixel 3a
compatible=google,sargo qcom,sdm670
serial=99NAY1AZG1
CID=13014e53304a394b381011182ce76600
active consoles=tty0 ttyMSM0
```

This getty is an intentional unauthenticated physical root shell for a
sacrificial lab device, not a production-safe default. Distrobox shares the
host's home and devices and is not a security boundary. The raw UART journal
also contains identifiers and terminal bytes and should be reviewed before
publication.

The container has native fastboot, ADB, and socat access to the host devices.
It does not have `usbutils`; inspect topology without changing the container
or leaving the canonical command surface with:

```sh
distrobox enter --name fedora-latest -- \
  distrobox-host-exec /usr/bin/lsusb -t
```

## Direct-cable USB-IN failure and patch boundary

The first direct cable did not merely enumerate slowly. From 09:48:32 through
09:49:08 the Deck logged a continuous series of high-speed resets for device
43 at `3-1.1`. After the cable swap, device 44 enumerated cleanly at 09:51:13
as `1d6b:0104`, product and manufacturer `pocketboot`, serial `99NAY1AZG1`.
That instance disconnected at 09:52:00 without another reset storm. The new
cable removed one transport fault, but it did not fix the device-to-host DWC3
queue failure described below.

Bounded tests on the unpatched image separated payload size from payload
source:

| Device-to-host path | 64 KiB | 4 MiB | Observation |
|---|---:|---:|---|
| Sequential ADB output | passed | passed | Unpatched ADB can carry both sizes when writes remain sequential. |
| Synthetic fastboot staged data | passed | passed | A fastboot upload of this size can succeed; size alone is not the trigger. |
| Real `userdata` fastboot staging/upload | not needed | failed | Reading and staging completed, but the upload wedged on USB IN. |

The real test read exactly 4 MiB from `userdata`; it did not write the block
device. At about kernel time `t=227s`, UART recorded five
`dwc3 ... was not queued to ep3in` errors. PocketBoot then reported
`timeout waiting for exact AIO transfer` and its userspace fastboot server
exited. This is the repeatable failure signature. No received file from that
attempt is backup evidence.

`gadgetry-most-foul` defaults the endpoint direction to a queue depth of 16
and splits a logical write into 16 KiB AIO requests. PocketBoot's fastboot
transfer buffer remains 1 MiB, so one upload write can present many requests
to the endpoint concurrently.

The first patched image, SHA-256
`fd065c95adb6a0dcfe7555b54573061c79bf7435a9f92c320932a786661d7586`, changed
only the fastboot device-to-host queue depth to one. A 4 MiB real-userdata
upload still failed: the Deck's fastboot process returned `Protocol error`,
and PocketBoot timed out one incomplete exact-AIO request after 30 seconds.
The five `was not queued to ep3in` messages were gone, so serialization fixed
the concurrent-queue rejection but was not sufficient for this link.

The current focused
[`0003-serialize-fastboot-upload-writes.patch`](../patches/pocketboot/0003-serialize-fastboot-upload-writes.patch)
keeps `queue_len=1` and slices every fastboot payload write into 4 KiB logical
writes, matching the write size that survived the ADB probes. It does not
change ADB, mass storage, fastboot host-to-device downloads, response packets,
or any storage command. This combined version still requires hardware
validation before trusting a large readback.

The complete six-patch tree was cross-built inside the Deck's
`fedora-latest` distrobox as tree
`07bf6258f893d89b9c11c5db8063632246c1b0a4`. The resulting image is
`/home/deck/frankensargo-lab/pocketboot-current/pocketboot-sargo-lab.img`,
SHA-256
`ad37af96fe9620e3600337f0dfe8a76fe47abcc48013398fb72ec8919be05cd8`.
That build success is not a hardware readback result.

The failed queue-only probe also exposed a separate recovery defect: a fatal
fastboot error let PocketBoot PID 1 return, so Linux panicked while tearing
down the gadget. The final
[`0006-hold-pid1-for-recovery.patch`](../patches/pocketboot/0006-hold-pid1-for-recovery.patch)
keeps PID 1 alive after any coordinator return. Successful kexec and reboot
actions do not return; the hold path exists so UART getty and Magic SysRq stay
available after a failed or empty boot attempt.

Across all of these tests, `userdata` retained its original identity and
content state: it was only read, never mounted read-write, erased, formatted,
or used as an output target. In particular, no `pvcreate` or other LVM write
has occurred.

## If USB/IP is retried

1. Discover the current PocketBoot bus ID (`3-1.4` was historical) and
   export/import only that device; do not import the FTDI UART.
2. Forward UART separately with SSH/socat on the Deck.
3. Prevent the imported PocketBoot mass-storage interface from binding to
   desktop `usb-storage`, or build a remote-lab image without UMS.
4. Confirm exact USB serial before ADB/fastboot access.
5. Run one bounded inventory and explicitly close ADB before detaching.
6. Unbind on the Deck first, then confirm the desktop VHCI port disappears.
7. Keep a local kernel log and Deck `usbipd --debug` trace for the whole run.

Do not retry with both exports merely because they enumerate. Do not reboot or
send SysRq to the phone as a remedy for a host-side VHCI/SCSI wedge.

## Later direct-cable PBREAD boundary and recovery failure

The later direct-cable work used image SHA-256
`26c357405f551ec889ec2f5e759816a412cefd132cdc076ea856a5f64f5c1c2e`,
patched PocketBoot tree
`068cb8ef4b4203e367647abef5a594ff07c83d14`, and patches 0001 through
0009. Its Android v2 command line
contained `pocketboot.log=debug pocketboot.acm sysrq_always_enabled=1`.
Patch 0009 made every configured UMS LUN read-only. UART independently confirmed
serial `99NAY1AZG1`, slot `_a`, and retry count 2. This was the third and most
recent same-slot retry reset; avoid another unless ABL genuinely refuses a
transient boot.

Small PBREAD responses worked, but the link had a sharp cumulative USB-IN
boundary. Every test below read synthetic or real source bytes and wrote no
phone block device:

| Target/host control | Passing total bytes | First observed failure |
|---|---:|---:|
| Original PBREAD path | 1,024; 4,608; 16,896 | 33,280 (`EPROTO`) |
| Host fastboot capped to 1 KiB usbfs reads | 16,384; 16,896; 17,408 | 24,576 (`EPROTO`) |
| Experimental 64 KiB target AIO | — | 24,576 (`EPROTO`) |
| ADB | 512 | one 16 KiB transfer |
| Read-only kernel UMS | — | approximately 16 KiB |

The 1 KiB host binary is retained at
`/home/deck/frankensargo-lab/fastboot-1k/fastboot`, SHA-256
`9a209ae867860c3e09d4a989476bde5ae590fa2ad93a704c185fcee34c08d58a`;
that directory also contains its source, patch, RPM, and notes. Because the
24,576-byte probe still failed with correctly capped host reads, host usbfs
read size is falsified as the primary cause.

The final failing probe exposed a deterministic target-side teardown deadlock.
PocketBoot timed out after 30 seconds, while the ACM kernel-log forwarder still
owned or reopened `/dev/ttyGS0`. UART SysRq task output showed PocketBoot in
uninterruptible sleep at:

```text
gserial_free_port
gserial_free_line
acm_free_instance
usb_put_function_instance
configfs_rmdir
```

SysRq SAK worked but killed the only UART shell, and that image did not respawn
its getty. PID 1 and the kernel remained alive, but USB fastboot/ADB/ACM were
gone and no userspace recovery path remained. The target therefore requires a
physical reset back to ABL before another control. Do not use SysRq immediate
reboot for that transition: it may enter the active Android slot rather than
ABL.

Patch 0011 now stops and joins ADB, FunctionFS, and kmsg workers before
configfs teardown, refuses removal unless tty closure is proven, bounds the
teardown in a disposable worker, and gives PID 1 a respawning Rust getty
supervisor. Patch 0012 adds `tx-fifo-resize` to the Sargo DWC3 node. Google’s
shipping SDM670 tree carried that property, while the pinned tree did not;
without it, DWC3 does not redistribute TX FIFO space for PocketBoot’s five IN
endpoints. This is the strongest current initial-transfer hypothesis, distinct
from the proven ACM teardown defect, and still requires a fresh-boot hardware
comparison.

The corresponding ACM-plus-FIFO-resize comparison image is staged at
`/home/deck/frankensargo-lab/acm-txfifo-safe-teardown-fc3a32d/`. Its exact
0001-through-0012 tree is
`fc3a32d120f912c084621799f625b5c168e7b604`, the image SHA-256 is
`4b628a5c442dac90caedbcda2a6dfb1955ed4a9205afb0be0a4de99645964c51`,
and its parsed command line is exactly
`pocketboot.log=debug pocketboot.acm sysrq_always_enabled=1`. The processed
DTB contains one `tx-fifo-resize` property. The directory contains checksum,
provenance, and independent verification files. These facts prove the build,
not the USB hypothesis; use it for one fresh-boot comparison only.

That fresh-boot comparison was completed from ABL. UART proved the exact image
and command line, `sysrq: sysrq always enabled`, and ABL retry count 1. A
PBREAD payload of `0x5e00` bytes produced a 24,576-byte staged envelope, then
failed on `get_staged` with the same host `EPROTO` and target 30-second exact
AIO timeout. DWC3 FIFO resizing alone therefore does not fix the initial
USB-IN transfer failure.

Patch 0011 did fix the recovery failure. It stopped and joined the kmsg, ADB
and fastboot workers, closed `ttyGS0`, and completed configfs teardown instead
of blocking in `gserial_free_port`. FunctionFS teardown emitted two kernel
`__flush_work` warnings, which remain a defect to investigate, but there was no
panic or D-state teardown. PID 1 entered its recovery hold with UART usable.
An interactive `id` returned `uid=0 gid=0`; exiting that shell caused the PID
1 supervisor to spawn a replacement getty one second later. No phone storage
write occurred.

An independently attested no-ACM control image is staged on the Deck at:

```text
/home/deck/frankensargo-lab/pocketboot-control-noacm/
  pocketboot-sargo-lab-noacm-b542.img
  pocketboot-sargo-lab-noacm-b542.img.sha256
  pocketboot-sargo-lab-noacm-b542.img.provenance
```

Its SHA-256 is
`f1e7f2e793f2a3ce6c7a7e868584720037e7867bebc2d397bbe0f8e7e05b294f`,
and its parsed Android v2 command line is exactly
`pocketboot.log=debug sysrq_always_enabled=1`. It differs from the prior
0001-through-0008 source tree only by removing `pocketboot.acm`. Use each
matrix image only once from a fresh ABL boot; the first `EPROTO` can poison
endpoint state and invalidate subsequent comparisons.

Prefer its later safe-teardown successor for the actual no-ACM control:

```text
/home/deck/frankensargo-lab/noacm-txfifo-safe-teardown-fc3a32d/
  pocketboot-sargo-lab-noacm.img
  pocketboot-sargo-lab-noacm.img.sha256
  pocketboot-sargo-lab-noacm.img.provenance
  pocketboot-sargo-lab-noacm.img.verification
```

That image is 7,856,128 bytes with SHA-256
`3e5fa16aac14624cec4a9034031eacab2e88b0081d59749c74444458b1357aa5`.
It retains the exact 0001-through-0012 base, FIFO overlay, read-only UMS,
bounded teardown and respawning getty; its only source delta from tree
`fc3a32d120f912c084621799f625b5c168e7b604` removes `pocketboot.acm`, producing
control tree `8cff350dd74cb5ee3d238085163a7171fdd69d85`. Its parsed command line is
exactly `pocketboot.log=debug sysrq_always_enabled=1`.

No DRM panic appeared during this failure. If a future run shows PocketBoot’s
panic screen or compressed QR code, stop before resetting and photograph the
entire display and QR. That evidence is more useful than another blind retry.

Patch 0013 subsequently added the raw standard ADB `shell_v2` protocol and
typed child exit status required by the takeover/import controllers. It is
source- and host-test validated as exact patch-stack tree
`fb96a55f631bc9ebd1dd7b0c97874fd9050201a9`; the patch SHA-256 is
`bbad5706d31665c55dbe60088ae20e3d379addb6ad2eca87295f09ae429eaaa2`.
It is not present in either staged comparison image above. Do not use those
0001-through-0012 images for any command whose success would authorize a
storage mutation; the next mutation-capable image must contain this exact
0001-through-0013 tree and separately prove `shell_v2` with the nonce/status
probe before controller preflight.

Rebuild the next generic no-ACM control from that current patch stack with
`bin/build-pocketboot --no-acm`. The reproducible outputs are
`out/pocketboot-noacm/pocketboot-sargo-lab-noacm.img` plus `.sha256`,
`.provenance`, `.profile.json`, and final `.bundle.json` sidecars. Require
`python3 lib/pocketboot_bundle.py verify --manifest ...bundle.json`; the
completion manifest is linked and directory-fsynced only after every other
fresh-destination member is durable. A partial directory without that marker
is not an artifact. The profile leaves the source tree unchanged and proves
its artifact differs from the parent image only within the two Android-v2
cmdline fields. Do not hand-edit the Sargo TOML or reuse the historical
`b542`/`fc3a32d` control as mutation-capable evidence. Once the VG exists, use
`bin/build-pocketboot-bound --no-acm` so the transport profile and observed
storage binding are composed in one restored, provenance-recorded build.

That current 0001-through-0013 no-ACM bundle has now been built and verified
on `sam-desktop` under ignored workspace output
`out/pocketboot-0013-noacm-bundle/`. The image is 7,864,320 bytes with SHA-256
`08ed581376cb5d95e6ffa9c3df575848e9240bafdeb39c1e66e6b2b0144a90a7`;
its completion manifest SHA-256 is
`793ed797ccb9904f81bddb28fb9958386fed7bbb1f5e72343fc099e58320d2cb`.
Its parent ACM image is `20375ae6…`, and an independent byte comparison found
exactly 37 changed bytes at zero-based span `[85,122)`, all inside the first
Android-v2 cmdline field. The strict manifest verifier, profile tests, bound
builder tests, full PocketBoot 188-test suite, and xtask 28-test suite pass.
This bundle has not yet been transferred to the sleeping Deck or booted on
Frankensargo; its USB and hardware `shell_v2` behavior therefore remain the
next qualification, not an inferred success.
