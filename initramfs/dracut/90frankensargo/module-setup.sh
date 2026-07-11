#!/bin/sh

# Dracut module interface.
# shellcheck disable=SC2154 # moddir is supplied by dracut.

check() {
    require_binaries dmsetup blockdev od udevadm || return 1
    return 0
}

depends() {
    echo lvm
    return 0
}

installkernel() {
    instmods dm_mod
}

install() {
    inst_multiple dmsetup blockdev od udevadm

    inst_simple "$moddir/map-userdata.sh" \
        /usr/libexec/frankensargo-map-userdata
    inst_hook initqueue/settled 60 "$moddir/map-userdata-hook.sh"

    config_source="${dracutsysrootdir:-}/etc/frankensargo-map.conf"
    if [ -f "$config_source" ]; then
        inst_simple "$config_source" /etc/frankensargo-map.conf
    fi
}
