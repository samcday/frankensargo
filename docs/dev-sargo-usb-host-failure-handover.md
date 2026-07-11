# dev-sargo USB host-mode bulk-OUT failure handover

Status: captured and cleaned up on 2026-07-11. The failing transport was
isolated below USB/IP and fastboot's large/asynchronous write path, but the
physical root cause is not yet known.

PocketBoot never executed. No partition was flashed, erased, reformatted,
resized, switched, or otherwise modified. Every image attempt used
`fastboot boot`; all of them stopped during its volatile download phase before
fastboot could send the subsequent `boot` command.

## Outcome

Small fastboot control transfers worked reliably through `dev-sargo`, but a
sustained high-speed bulk OUT transfer to downstream frankensargo
progressively stopped making forward progress.

The decisive capture removed USB/IP, networking, scatter/gather, large URBs,
and asynchronous queue depth from the data path. A local ARM64 fastboot process
on `dev-sargo` submitted one 16 KiB URB at a time:

| Payload URB | Completion | Elapsed |
| --- | ---: | ---: |
| 1 | 16,384 / 16,384 bytes, status 0 | 0.699077 s |
| 2 | 16,384 / 16,384 bytes, status 0 | 1.807506 s |
| 3 | 1,536 / 16,384 bytes, status `-2` | 5.431687 s |

The third URB's `-2` is `-ENOENT`: the bounded test unlinked it after progress
had already collapsed. The matching xHCI trace records `xhci_urb_dequeue`
immediately before the partial giveback. It is cancellation evidence, not a
controller-reported CRC, protocol, or stall error.

Only 34,304 of 7,831,552 payload bytes (0.438%) were delivered. No `boot`
command followed.

## Topology and identity

Two Pixel 3a phones were involved and must not be confused:

```text
desktop host
  | SSH control; optionally USB/IP data forwarding
  v
dev-sargo                         future daily driver, not the fastboot target
  | sdm670 DWC3/xHCI host path
  | physical USB-C
  v
frankensargo                      the only fastboot target
```

During the failing session:

| Item | Observation |
| --- | --- |
| `dev-sargo` OS | PocketFed/Fedora Rawhide image |
| `dev-sargo` kernel | `7.1.2-0.fc45.aarch64` |
| captured boot ID | `20b89671-236b-4967-bdc0-f0260eb36f87` |
| LAN address | `192.168.0.151` (observation only) |
| USB controller | `xhci-hcd.1.auto`, xHCI 1.10, MMIO `0x0a600000` |
| controller interrupt | IRQ 131 / GICv3 165 |
| target USB identity | `18d1:4ee0`, high speed, bus 1 device 2 |
| target fastboot identity | serial `99NAY1AZG1`, `product=sargo`, slot `a` |
| target state | unlocked `yes`, secure `yes` |
| session bus ID | `1-1` (ephemeral and never an identity) |

The IP address, boot ID, bus ID, and kernel device number are session facts,
not authority. Every fastboot operation was scoped with
`-s 99NAY1AZG1`; the repository probe also requires the bootloader-reported
`serialno` to equal the requested serial exactly.

After this capture, `dev-sargo` was returned to USB device/sink mode for
charging. Frankensargo was moved to a direct desktop connection. A
read-only direct probe then succeeded with the same serial and product, slot
`a`, battery voltage 4168 mV, and no VHCI import. Sustained direct-desktop bulk
OUT has not yet been tested, so it is the next control rather than an already
proven result.

## Isolation matrix

All image attempts used the same 7,831,552-byte PocketBoot image with SHA-256
`98983cc3331de0f08d6a578b89f87f2b5003607e30cb7ae5d218eb56612d48a6`.

| Host and transport | Fastboot write model | Observation | What it excludes |
| --- | --- | --- | --- |
| Desktop fastboot 35 through USB/IP | two queued 256 KiB usbfs URBs | apparent first-window stall around 512 KiB | established the initial fingerprint, not its cause |
| ARM64 fastboot 35 directly on `dev-sargo` | two queued 256 KiB usbfs URBs | blocked in `USBDEVFS_REAPURB` | USB/IP and the network are not necessary |
| Patched desktop fastboot 35 through USB/IP | two queued 16 KiB URBs | stalled after roughly 64 KiB of TCP input | 256 KiB URB size is not the sole cause |
| Google Platform Tools 34.0.5 through USB/IP | synchronous 16 KiB writes | stalled after roughly 49 KiB of TCP input | asynchronous fastboot 35 queueing is not the sole cause |
| Fedora/EPEL ARM64 fastboot 33 directly on `dev-sargo` | synchronous, one 16 KiB non-SG URB | trace-backed progressive collapse shown above | USB/IP, networking, SG, async depth, and slow userspace requeue |

The first two 16 KiB payload URBs in the decisive run were followed by the
next submission in 0.643 ms and 0.384 ms respectively. Userspace was not
pausing between transfers.

The download handshake was healthy:

```text
host -> target: download:00778000
target -> host: DATA00778000
```

The 17-byte command completed in 3.789 ms and the 12-byte reply in 27.583 ms.
The accepted length exactly matched the image size.

## Controller evidence

The xHCI interrupt count advanced from 152 to 157 during the eight-second
capture. No USB/xHCI fatal error, reset, disconnect, or global suspend/resume
was logged in the transfer window. This proves that the interrupt path was not
completely dead; it does not exclude a delayed or missed transfer event.

The target, xHCI child, and DWC3 parent were contemporaneously observed with
runtime-PM `control=on`, `runtime_status=active`, and zero accumulated runtime
suspend for the target. Those sysfs snapshots were not saved into the trace
bundle, so treat them as session notes rather than independently preserved
evidence.

The runtime xHCI quirk mask was `0x0000808002000010`. In the matching source it
decodes to:

- `XHCI_SPURIOUS_SUCCESS`;
- `XHCI_BROKEN_PORT_PED`;
- `XHCI_SG_TRB_CACHE_SIZE_QUIRK`; and
- `XHCI_WRITE_64_HI_LO`.

The relevant DWC3 host properties are therefore present and parsed. A missing
`xhci-sg-trb-cache-size-quirk` is not a good explanation for this capture, and
the decisive URBs had `sgs 0/0` anyway.

Host mode itself is new downstream work. The source series adds PM660 Type-C
support in commit `d29b357303d0422c12b63509c810c69166e2d60d` and changes
sargo from peripheral-only to OTG/role-switch operation in commit
`e98ba6eb5eee09f9489faf311ebef070ce58b9d9`. The running device tree is
high-speed-only through the QUSB2 PHY; it does not exercise a SuperSpeed PHY.

A contemporaneous register dump identified DWC3 revision 3.00a and showed the
USB2 `SUSPHY` and `ENBLSLPM` bits clear, consistent with the device-tree
low-power quirks. Together with the active runtime-PM observations, an
inadvertent PHY low-power transition is a weaker lead than host power or
system churn. The register dump was not included in the preserved trace bundle.

## Important confounders

This evidence localizes the symptom to the physical downstream host system,
but it does not distinguish among xHCI, DWC3, PHY, Type-C/role handling, VBUS,
cable/adapter integrity, the target bootloader, or unrelated system-load
effects.

In particular, the preserved kernel log contains 88,590 repeated
`wwan0at0`/`wwan0at1` attach/disconnect messages between monotonic timestamps
45.901521 and 1281.544081. There were 545 such messages during the 7.970-second
USB capture itself, about 68 per second. This churn is a major confounder and
must be quiesced or traced alongside the next reproduction.

The running COPR kernel appears, with high confidence but without an on-device
source attestation, to correspond to local checkout commit
`ef5c4ce9072428b7b8294cd874bbc25c9e2e3513` (`VIBES: rpmsg: glink: defer
rpmsg device removal from destroy_ept`, dated 2026-07-10). Compare or revert
that change when investigating the WWAN churn; do not yet claim it caused the
USB failure.

The strongest hardware-side lead is source VBUS. The experimental device tree
uses PM660 GPIO6's fixed external 5 V boost as `vdd-vbus` and disables PD. The
local bring-up notes explicitly say that this supply path has not been
hardware-validated. Its software `get_vbus` state can report the boost as
enabled without proving delivered voltage under load. The unchanged PMIC
`usb-plugin` and `usbin-icl-change` interrupt counters do not measure analog
droop. Measure VBUS/current or insert a powered hub before trying speculative
xHCI quirks.

Other unclosed confounders are:

- `dev-sargo` was around 34% battery according to the operator's newly working
  UPower reporting;
- the same physical cable/adapter was used for the downstream tests;
- USB/IP modules had been loaded earlier in the decisive boot, although the
  target was unbound and USB/IP was absent from the captured data path; and
- upstream sargo device-tree support still describes this controller as
  peripheral-only, so this custom host mode is bring-up territory.

## Preserved evidence

The bundle is under
[`out/traces/frankensargo-usb-trace-20b89671`](../out/traces/frankensargo-usb-trace-20b89671/):

| File | SHA-256 |
| --- | --- |
| `xhci.trace` | `7776ee8082f1f6a721415412d07885a77a452944ffe93388a560f87afb7eb56a` |
| `server.usbmon` | `ffbe7971b49bc260e00105d30c942cf568e52678df628c55ff28b8153b07a87b` |
| `kernel.log` | `87f8170ec41de0acf63de7003ff60a12ec903f83653a268b5cb4831806968364` |
| `interrupts.before` | `f16e5c0a0c112de56d3b343c734e82d973ce5f33bb1e5504d2e85b5a9cfc8121` |
| `interrupts.after` | `45b01d4667efcd42c8320639c9ce0679da803fcabde985f205dea4bf4887895a` |

`out/` is intentionally gitignored. Copy this bundle separately when handing
the report to someone who does not share this workspace.

Transport variants are also preserved under `out/tools/`:

| Artifact | SHA-256 |
| --- | --- |
| Fedora 44 patched fastboot 35 binary | `f430d1e28f742fdadaf883cbc236a730a305e5a7c1aff9b83b07fbb22c0dda07` |
| its RPM | `a233cf1806d4f1a727ef399e520cc572a626e20c5ed073a0c115d6d0f505428f` |
| Google Platform Tools 34.0.5 fastboot | `08d0f9f73405854401208e01657d10683818b0ccf8ab548d4a287135bb0b3e15` |
| Google Platform Tools 34.0.5 archive | `362f8f6218af0f4c61e5aaafb8e255a426c7a0ee00127dfab7371775081d3124` |
| Fedora/EPEL ARM64 fastboot 33 RPM | `2c3769c976bd8b1332a7842093070b5cc482491c46f6dfbd43c458643796b5ad` |

## Safe continuation

The next experiments, in order, are:

1. **Direct-desktop positive control.** With frankensargo directly
   connected, repeat the exact-serial probe, then perform an explicitly
   authorized bounded bulk download using a known-good cable. A successful
   `fastboot boot` will execute PocketBoot, so do not treat it as read-only.
2. **Charge and simplify `dev-sargo`.** Reboot it, never load USB/IP during the
   control run, confirm host role and adequate battery/VBUS, inhibit sleep, and
   stop the WWAN attach/disconnect storm before tracing. Prefer the parent
   kernel commit `0ef256c2afb0d35cb752c8102be57182b58198c0` or disable
   `rpmsg_wwan_ctrl` for this comparison.
3. **Change the physical path.** Compare another cable and an externally
   powered high-speed hub. Record VBUS/current/role and UPower events rather
   than inferring power health from enumeration.
4. **Use a download-only harness.** Sweep payload and chunk sizes without a
   command that will boot or flash if transport unexpectedly recovers. Useful
   boundaries are 512 B through 16 KiB per transfer and payloads above 64 KiB.
5. **Compare kernels.** Reproduce on the parent of the suspected RPMsg change,
   then bisect only after system churn and physical-power variables are under
   control.

Every new session must re-establish the boot ID, kernel, USB role, exact target
serial/product, physical bus path, charge state, and absence of stale VHCI
imports. Do not trust a cached `1-1` bus ID. Never use an unscoped
`fastboot devices` result as authority.

The exact captured `fastboot boot` command is intentionally not presented as a
safe default reproducer: if the bug disappears, that command will execute the
downloaded image. Storage-writing commands, slot changes, reboot commands, and
factory-image tools remain out of scope.

## Cleanup and current state

At the end of the downstream session:

- the local VHCI import was detached;
- remote `usbipd` was stopped;
- the temporary nftables table was removed;
- `usbip_host` and `usbip_core` were unloaded;
- the blocking sleep inhibitor was stopped;
- the tracefs instance was removed after capture; and
- `dev-sargo` reported USB role `device` after the operator switched it to
  sink mode for charging.

Temporary RPMs, extracted tools, and the image remain under
`/var/tmp/frankensargo-{usbip,fastboot}` on `dev-sargo`; they are not installed
into its immutable `/usr`. Remove them deliberately when no longer useful.

Frankensargo is now directly attached to the desktop and passes the
hardened read-only probe. No direct-desktop image download has yet been sent.

## Primary references

- [AOSP fastboot 35.0.2 Linux USB transport](https://android.googlesource.com/platform/system/core/+/90e4908e776d2be9b26b87af649e63e4208cf085/fastboot/usb_linux.cpp#91)
- [AOSP fastboot asynchronous write loop](https://android.googlesource.com/platform/system/core/+/90e4908e776d2be9b26b87af649e63e4208cf085/fastboot/usb_linux.cpp#419)
- [Platform Tools 34.0.5 synchronous Linux transport](https://android.googlesource.com/platform/system/core/+/refs/tags/platform-tools-34.0.5/fastboot/usb_linux.cpp#403)
- [Linux usbmon documentation](https://docs.kernel.org/usb/usbmon.html)
- [Linux USB completion error codes](https://docs.kernel.org/driver-api/usb/error-codes.html)
- [Upstream sargo USB device-tree context](https://github.com/torvalds/linux/blob/master/arch/arm64/boot/dts/qcom/sdm670-google-common.dtsi)
