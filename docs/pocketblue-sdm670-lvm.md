# PocketBlue sdm670 on frankensargo LVM

This is the first loader-neutral sdm670 bring-up. It follows PocketBlue's
ordinary Qualcomm layout: a FAT ESP, an ext4 XBOOTLDR filesystem containing a
Type #1 BLS entry, and a Btrfs root filesystem. It deliberately contains no
ABlx, `qbootctl`, direct-ABL deployment, or writes to Android `boot_a` or
`boot_b`.

On 2026-07-13 this image booted successfully from three LVs on frankensargo
`99NAY1AZG1`. PocketBoot discovered its BLS entry on XBOOTLDR, and the
downstream Fedora deployment reached UART getty with working Magic SysRq.

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
739979f sdm670: document all LVM activation requests
d058d19 sdm670: install dynamic partition mapper
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

The imported inputs are:

| Image | Bytes | Filesystem UUID | SHA-256 |
|---|---:|---|---|
| `fedora_esp.raw` | 268,435,456 | `7FD8-FC69` | `19a05d85d8a26bf223e945e7dd8b54dd3e661ac69362e63328a37f2b368bf225` |
| `fedora_boot.raw` | 1,073,741,824 | `da6c5411-07c8-4228-a261-8ff0c478b650` | `6789ed1a710e7b63e5b73aea6c5dedc22c9d1bd3696da9506c8597403b8c8fd2` |
| `fedora_rootfs.raw` | 9,137,274,880 | `dbaf8d57-2ac8-4a8d-a621-4a493851c348` | `6845c540f146c41dadeb493ec5117401cbec5b109a1b9213490335a41eb0eec8` |

The XBOOTLDR image passed `e2fsck -fn`. The root image is Btrfs with the BLS
entry's `rootflags=subvol=/root`. The staged XBOOTLDR hash above is final and
includes exact activation arguments for root, XBOOTLDR, and ESP.

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
rd.lvm.lv=franken/pocketblue-esp
```

The latter two requests make `/boot` and `/boot/efi` independent of a later
udev autoactivation pass; PocketBoot's own device-mapper activation does not
survive kexec. This is the portable distro-side proposal: include dracut's
stock `lvm` module and `lvm2` userspace, keep filesystem-UUID roots, and let the
deployment/loader supply the site-specific `rd.lvm.lv=` arguments. The
argument is LV-name-bound, not VG-UUID-bound; a future manifest-aware
PocketBoot policy can add stronger identity checks before constructing it.

The prepared BLS entry also replaces the generic `ttyS0` with sargo's UART and
enables downstream recovery:

```text
console=tty0 console=ttyMSM0,115200n8
sysrq_always_enabled=1
```

Its kernel, initramfs, and `fdtdir` all live below the XBOOTLDR filesystem, so
PocketBoot does not need the ESP to launch it.

The hash-bound XBOOTLDR currently has one entry, `ostree-1.conf`, and no
`loader.conf`. PocketBoot sorts XBOOTLDR ahead of ESP and nested-disk entries;
the literal PocketBlue options contain no unresolved `$kernelopts`, so this
sole entry is eligible for automatic boot. After discovery, an exact-serial
`fastboot continue` exits PocketBoot's fastboot server and boots the first
automatic entry. This gives this first-pass image a deterministic no-touch
launch path while retaining the on-device menu. If a later image contains
multiple entries, it should add `default ostree-1.conf` to
`loader/loader.conf` before its final image hash is recorded.

## Verified hardware result

The three source images were copied into thick LVs, synced, and independently
read back with the exact SHA-256 values in the table above. Staging mappings
were then deactivated before publication. The observed LV identities are:

| LV | Size | LV UUID | Observed downstream use |
|---|---:|---|---|
| `pocketblue-esp` | 256 MiB | `cCKrT3-jw0s-K6mv-oazS-4M58-4NQv-jcb2Np` | vfat at `/boot/efi` (rw) |
| `pocketblue-xbootldr` | 1,024 MiB | `Ye4q2D-QYao-k25n-Yoio-hTis-y1Of-06dQ9h` | ext4 at `/boot` (ro) |
| `pocketblue-root` | 8,716 MiB | `eS20xg-PD3X-faNX-36DH-t8wP-zxvE-Gs0R1i` | Btrfs OSTree backing |

`lsblk` showed the root mapper backing `/sysroot`,
`/sysroot/ostree/deploy/default/var`, `/var`, and `/etc`. The deployed `/`
reports `composefs`; it is therefore deliberately not described as a direct
mount of the root LV. All LVs retain
`distro.pocketblue,greygoo.replaceable`, but only XBOOTLDR carries
`pocketboot.bootfs.v1`.

The successful no-ACM bound PocketBoot image is 7,864,320 bytes with SHA-256:

```text
988ba0fb069f1dc6ae88c0267f6ccd267090da2acf5ef942d1da9bfe2e4df06c  pocketboot-sargo-lvm-bound-noacm.img
```

Bound builds now require the exact observed serial as well as the storage
identity; this preserves `androidboot.serialno` across a PocketBoot self-kexec:

```sh
bin/build-pocketboot-bound --no-acm \
  --serialno 99NAY1AZG1 \
  --vg-uuid 8Lobll-Ri4f-ilPQ-ptQh-Qmz7-xYSe-2Yo006 \
  --pv-partuuid db04e713-11c3-4d68-bec2-8cc483bd3891 \
  --kernel-tree "$PWD/.work/linux-sdm670-pinned" \
  --output-dir out/pocketboot-bound-franken-0014-serial
```

The first read-only discovery exposed LVM's JSON `lv_active=-1` sentinel.
PocketBoot patch `0014-accept-readonly-unknown-lv-active.patch` accepts that
unknown state while retaining the independent sysfs fence against pre-existing
device-mapper mappings. With that patch, PocketBoot activated only the tagged
XBOOTLDR LV read-only and discovered `ostree-1.conf`.

After `fastboot -s 99NAY1AZG1 continue`, the downstream command line contained
exactly one activation request for each filesystem and retained recovery:

```text
rd.lvm.lv=franken/pocketblue-root
rd.lvm.lv=franken/pocketblue-xbootldr
rd.lvm.lv=franken/pocketblue-esp
console=tty0 console=ttyMSM0,115200n8
sysrq_always_enabled=1
```

UART then proved active `serial-getty@ttyMSM0.service` and
`systemd-homed.service`. Sending a BREAK followed by `h` produced the kernel
SysRq help text without resetting the phone. This completes the first
root-on-LVM, PocketBoot-to-PocketBlue boot proof; no Android GPT or
`boot_a`/`boot_b` partition was changed.

The booted deployment reported image
`ghcr.io/samcday/sdm670-google-sargo-tty:rawhide-sdm670-frankensargo`, digest
`sha256:a012a6523634a7c383cf70a27f3a3581adeb9ad0f56939f821b1553133fb8f90`,
and version `45.20260712.0`.

## Known non-blocking issues

- PocketBoot emits FunctionFS teardown WARNs while handing USB over for kexec;
  they did not prevent discovery or the downstream boot.
- `droid-juicer.service` is degraded because
  `make-dynpart-mappings@system_a.service` and
  `make-dynpart-mappings@system_b.service` are absent, ending in `Failed to map
  super partition`. Commit `d058d19` installs the missing
  `make-dynpart-mappings` package for the next image; this booted image predates
  that fix.
- Consequently the extracted Android firmware is unavailable: modem remoteproc
  cannot load `qcom/sdm670/sargo/mba.mbn`, while the GPU cannot load
  `qcom/sdm670/sargo/a615_zap.mbn` and hardware initialization fails. These are
  distro integration gaps, not failures of the LVM boot path.
