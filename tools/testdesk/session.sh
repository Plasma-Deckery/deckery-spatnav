#!/bin/sh
# Runs inside the nested session: publish its environment, then idle until
# stop.sh kills the session.
STATE="${XDG_RUNTIME_DIR}/deckery-testdesk${DECKERY_TESTDESK:-}"
# D-Bus activation of org.a11y.Bus fails here ("Permission denied"): the
# real session starts it via systemd, which dbus-run-session doesn't have.
/usr/libexec/at-spi-bus-launcher --launch-immediately > "$STATE/atspi.log" 2>&1 &
for i in $(seq 1 25); do
    dbus-send --session --print-reply --dest=org.freedesktop.DBus / \
        org.freedesktop.DBus.NameHasOwner string:org.a11y.Bus 2>/dev/null | grep -q true && break
    sleep 0.2
done
# Same for the registry on the a11y bus: its activation goes through the
# systemd unit that already belongs to the real session ("unit failed").
/usr/libexec/at-spi2-registryd > "$STATE/registryd.log" 2>&1 &
# The real session has this on already (set by the overlay/Orca); apps only
# expose their tree if it's on before they start.
for prop in IsEnabled ScreenReaderEnabled; do
    dbus-send --session --dest=org.a11y.Bus /org/a11y/bus org.freedesktop.DBus.Properties.Set \
        string:org.a11y.Status string:$prop variant:boolean:true
done
i=0
while [ "$i" -lt "${DECKERY_TESTDESK_OUTPUTS:-1}" ]; do
    kscreen-doctor "output.Virtual-$i.scale.${DECKERY_TESTDESK_SCALE:-1.1}" > /dev/null 2>&1
    i=$((i + 1))
done
{
    echo "export DBUS_SESSION_BUS_ADDRESS='$DBUS_SESSION_BUS_ADDRESS'"
    echo "export WAYLAND_DISPLAY='$WAYLAND_DISPLAY'"
    echo "export DISPLAY='${DISPLAY:-}'"
    echo "export XDG_RUNTIME_DIR='$XDG_RUNTIME_DIR'"
    echo "export DECKERY_TESTDESK='${DECKERY_TESTDESK:-}'"
    echo "export XDG_CONFIG_HOME='$XDG_CONFIG_HOME'"
    echo "export XDG_DATA_HOME='$XDG_DATA_HOME'"
    echo "export XDG_CACHE_HOME='$XDG_CACHE_HOME'"
    echo "export XDG_STATE_HOME='$XDG_STATE_HOME'"
    echo "export QT_QPA_PLATFORM=wayland"
    echo "export QT_LINUX_ACCESSIBILITY_ALWAYS_ON=1"
    echo "export XDG_CURRENT_DESKTOP=KDE"
    echo "export KDE_FULL_SESSION=true"
    echo "export KDE_SESSION_VERSION=6"
} > "$STATE/env.tmp" && mv "$STATE/env.tmp" "$STATE/env"
exec sleep infinity
