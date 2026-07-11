#!/bin/sh

# A settled hook is retried as udev discovers the Android userdata partition.
if [ -x /usr/libexec/frankensargo-map-userdata ]; then
    /usr/libexec/frankensargo-map-userdata --hook
fi
