# Steam Deck remote-lab handover

This records the 2026-07-11 USB/IP experiment and the preferred 2026-07-12
Deck-local path from `sam-desktop` to frankensargo through `steamdeck`. No
phone partition was mounted or written.

## Topology and identities

- Steam Deck tailnet address during the run: `100.64.0.8`.
- Steam Deck kernel: `6.11.11-valve27-1-neptune-611-g60ef8556a811`.
- Frankensargo PocketBoot gadget during the USB/IP run: Deck bus ID `3-1.4`,
  USB `1d6b:0104`, serial `99NAY1AZG1`, five interfaces (ACM, fastboot, ADB,
  and mass storage). A replacement cable later put stock fastboot and the
  gadget on physical port `3-1.1`.
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
bootloader mode. This was observed, not repaired; no boot partition was
flashed.

A replacement cable exposed stock fastboot as `18d1:4ee0`, exact serial
`99NAY1AZG1`. `fedora-latest` is a Fedora 44 distrobox and could access the USB
device unprivileged. The actual transient boot used official fastboot
`37.0.0-14910828`, SHA-256
`76dde33fee8b1fd00bcaf2e7f94ddef6407f0beb5bc3a98a3d4127307af23f3a`,
from the shared staging directory. It downloaded the verified 7,831,552-byte
PocketBoot image and booted it without `flash`, `erase`, or a slot change.

Fedora's packaged `android-tools-35.0.2-17.fc44` was already installed and is
the preferred repeatable interface. A future transient boot can stay entirely
inside the distrobox after verifying the shared image:

```sh
sha256sum /home/deck/.local/share/frankensargo-lab/pocketboot-sargo-lab.img
distrobox enter fedora-latest -- \
  /usr/bin/fastboot -s 99NAY1AZG1 getvar product
distrobox enter fedora-latest -- \
  /usr/bin/fastboot -s 99NAY1AZG1 boot \
  /home/deck/.local/share/frankensargo-lab/pocketboot-sargo-lab.img
```

The required image digest is
`98983cc3331de0f08d6a578b89f87f2b5003607e30cb7ae5d218eb56612d48a6`.
After the boot, these checks passed from inside the same distrobox:

```sh
distrobox enter fedora-latest -- \
  /usr/bin/fastboot -s 99NAY1AZG1 getvar product
distrobox enter fedora-latest -- \
  /usr/bin/adb -s 99NAY1AZG1 get-state
distrobox enter fedora-latest -- \
  /usr/bin/adb -s 99NAY1AZG1 shell /usr/bin/id
distrobox enter fedora-latest -- \
  /usr/bin/adb -s 99NAY1AZG1 shell /bin/cat \
  /sys/block/mmcblk0/device/cid
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
distrobox enter fedora-latest -- \
  /usr/bin/socat -,rawer,echo=0 \
  /dev/serial/by-path/pci-0000:04:00.3-platform-xhci-hcd.2.auto-usb-0:1.3.4.3.1:1.0-port0,b115200,rawer,echo=0
```

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
