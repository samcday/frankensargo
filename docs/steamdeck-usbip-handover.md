# Steam Deck USB/IP handover

This records the 2026-07-11 tailnet control path from `sam-desktop` to
frankensargo through `steamdeck`. No phone partition was mounted or written.

## Topology and identities

- Steam Deck tailnet address during the run: `100.64.0.8`.
- Steam Deck kernel: `6.11.11-valve27-1-neptune-611-g60ef8556a811`.
- Frankensargo PocketBoot gadget: Deck bus ID `3-1.4`, USB `1d6b:0104`, serial
  `99NAY1AZG1`, five interfaces (ACM, fastboot, ADB, and mass storage).
- Frankensargo FTDI UART: Deck bus ID `3-1.3.4.3.4`, USB `0403:6001`.
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
reboot removes it.

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

## Next controlled retry

1. Export/import only PocketBoot bus ID `3-1.4`; do not import the FTDI UART.
2. Forward UART separately with SSH/socat on the Deck.
3. Prevent the imported PocketBoot mass-storage interface from binding to
   desktop `usb-storage`, or build a remote-lab image without UMS.
4. Confirm exact USB serial before ADB/fastboot access.
5. Run one bounded inventory and explicitly close ADB before detaching.
6. Unbind on the Deck first, then confirm the desktop VHCI port disappears.
7. Keep a local kernel log and Deck `usbipd --debug` trace for the whole run.

Do not retry with both exports merely because they enumerate. Do not reboot or
send SysRq to the phone as a remedy for a host-side VHCI/SCSI wedge.
