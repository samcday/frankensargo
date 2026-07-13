# PocketBlue sdm670 on frankensargo LVM

This is the first loader-neutral sdm670 bring-up. It follows PocketBlue's
ordinary Qualcomm layout: a FAT ESP, an ext4 XBOOTLDR filesystem containing a
Type #1 BLS entry, and a Btrfs root filesystem. It deliberately contains no
ABlx, `qbootctl`, direct-ABL deployment, or writes to Android `boot_a` or
`boot_b`.

The image is prepared and its storage/initrd contract is verified. Hardware
boot remains unproved while frankensargo is offline.

## Source and build

The ignored worktrees are:

```text
.work/pocketblue           frankensargo/sdm670-xbootldr
.work/pocketblue-packages  frankensargo/sdm670 (clean; no package change needed)
```

The PocketBlue branch is pushed to `samcday/pocketblue` at these commits:

```text
fdadf85 sdm670: add loader-neutral sargo image
d8400f2 sdm670: enable droid-juicer repository
277778e ci: keep disk images on mount-capable arm runner
```

Container run `29215523419` built the real sdm670 image successfully. Disk run
`29216264569` built and published the split images successfully. PocketBlue's
July switch to `ubuntu-26.04-arm` made bootc-image-builder's privileged nested
mount fail; the experimental branch keeps disk-image jobs on the previously
working `ubuntu-24.04-arm` runner.

The raw GitHub artifact is 1,614,039,303 bytes:

```text
df610c73b4cf6a84d22aa907476bdb144d0fb44017f5b6c44500cae64a0ae07c  pocketblue-sdm670-google-sargo-tty-rawhide-sdm670-frankensargo.7z
```

After the last-mile BLS edit, its inputs are:

| Image | Bytes | Filesystem UUID | SHA-256 |
|---|---:|---|---|
| `fedora_esp.raw` | 268,435,456 | `7FD8-FC69` | `19a05d85d8a26bf223e945e7dd8b54dd3e661ac69362e63328a37f2b368bf225` |
| `fedora_boot.raw` | 1,073,741,824 | `da6c5411-07c8-4228-a261-8ff0c478b650` | `93b9c27ab989a1ccba1a9c47cbc19d9aa88fae53ecb88fc1810c0624eeeaafd9` |
| `fedora_rootfs.raw` | 9,137,274,880 | `dbaf8d57-2ac8-4a8d-a621-4a493851c348` | `6845c540f146c41dadeb493ec5117401cbec5b109a1b9213490335a41eb0eec8` |

The XBOOTLDR image passed `e2fsck -fn`. The root image is Btrfs with the BLS
entry's `rootflags=subvol=/root`.

## Why the small LVM override works

The built initramfs is dracut 108 with the `lvm`, `dm`, `ostree`, and `btrfs`
modules. Inspection of the actual 60,267,731-byte initramfs proves it contains:

```text
/usr/bin/lvm
/usr/bin/lvm_scan
/usr/bin/pdata_tools
/usr/bin/thin_check -> pdata_tools
/etc/udev/rules.d/64-lvm.rules
/usr/lib/udev/rules.d/11-dm-lvm.rules
/var/lib/dracut/hooks/cmdline/30-parse-lvm.sh
```

It also contains the forced sargo storage/platform drivers; XBOOTLDR contains
`sdm670-google-sargo.dtb`. The BLS entry already names root by filesystem UUID,
so placing that filesystem directly in an LV does not require changing
`root=`. The loader only needs to append exact activation requests:

```text
rd.lvm.lv=franken/pocketblue-root
rd.lvm.lv=franken/pocketblue-xbootldr
```

The second request makes `/boot` available after the downstream kernel starts;
PocketBoot's own device-mapper activation does not survive kexec. This is the
portable distro-side proposal: include dracut's stock `lvm` module and `lvm2`
userspace, keep filesystem-UUID roots, and let the deployment/loader supply the
site-specific `rd.lvm.lv=` arguments. The argument is LV-name-bound, not
VG-UUID-bound; a future manifest-aware PocketBoot policy can add stronger
identity checks before constructing it.

The prepared BLS entry also replaces the generic `ttyS0` with sargo's UART and
enables downstream recovery:

```text
console=tty0 console=ttyMSM0,115200n8
sysrq_always_enabled=1
```

Its kernel, initramfs, and `fdtdir` all live below the XBOOTLDR filesystem, so
PocketBoot does not need the ESP to launch it.

## Current safe checkpoint

The owned VG remains `franken` on the userdata-anchor PV. Three thick LVs were
created before frankensargo was unplugged:

| LV | Size | Current role |
|---|---:|---|
| `pocketblue-esp` | 256 MiB | untagged for PocketBoot; contains a truncated failed first transfer |
| `pocketblue-xbootldr` | 1,024 MiB | unpublished; `pocketboot.bootfs.v1` was removed before import |
| `pocketblue-root` | 8,716 MiB | unpublished |

All retain `distro.pocketblue` and `greygoo.replaceable`. No filesystem image
is currently published to PocketBoot, and no GPT partition or Android boot
partition was changed. The interrupted transfer coincided with a Deck hub-wide
USB `-71` reset that removed both PocketBoot and the FTDI UART; it was detected
by a failed destination hash.

While dev-sargo is attached to the Deck, issue no USB, ADB, fastboot, UART, or
block-device command. Resume only after the user explicitly says frankensargo
is back and exact serial `99NAY1AZG1` is present.

## Resume the import

Run the writer inside Deck's `fedora-latest` distrobox. It verifies the complete
source hash first, compares every 64 MiB destination chunk, skips matches, and
retries an interrupted chunk up to 20 times. A larger LV is never truncated.

```sh
cd /home/deck/frankensargo-pocketblue

./write-lv-resumable 99NAY1AZG1 \
  images/fedora_esp.raw \
  /dev/mapper/franken-pocketblue--esp \
  19a05d85d8a26bf223e945e7dd8b54dd3e661ac69362e63328a37f2b368bf225

./write-lv-resumable 99NAY1AZG1 \
  images/fedora_boot.raw \
  /dev/mapper/franken-pocketblue--xbootldr \
  93b9c27ab989a1ccba1a9c47cbc19d9aa88fae53ecb88fc1810c0624eeeaafd9

./write-lv-resumable 99NAY1AZG1 \
  images/fedora_rootfs.raw \
  /dev/mapper/franken-pocketblue--root \
  6845c540f146c41dadeb493ec5117401cbec5b109a1b9213490335a41eb0eec8
```

Only after all three commands finish, add `pocketboot.bootfs.v1` to
`franken/pocketblue-xbootldr`, deactivate the three staging mappings, and reset
frankensargo so PocketBoot performs a fresh tagged-LV discovery. The ESP LV
stays untagged; it is retained to preserve PocketBlue's conventional update
layout.

Completion requires UART evidence from the downstream kernel showing the
prepared command line, successful activation and mount of
`franken/pocketblue-root`, a reached tty/getty, and working Magic SysRq. Until
those observations exist this bring-up is prepared, not boot-proven.
