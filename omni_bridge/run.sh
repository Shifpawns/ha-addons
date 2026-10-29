#!/usr/bin/with-contenv bashio

export SPEAKER_IP="$(bashio::config 'speaker_ip')"
export SPEAKER_NAME="$(bashio::config 'speaker_name')"
LMS_SERVER="$(bashio::config "music_server")"
[ "$LMS_SERVER" = "null" ] && LMS_SERVER=""
export LMS_SERVER
MA_PLAYER="$(bashio::config "ma_player")"
[ "$MA_PLAYER" = "null" ] && MA_PLAYER=""
export MA_PLAYER
export SYNC_DELAY_MS="$(bashio::config 'sync_delay_ms')"
export BITS_PER_SAMPLE="$(bashio::config 'quality')"
export MQTT_HOST="$(bashio::services mqtt 'host')"
export MQTT_PORT="$(bashio::services mqtt 'port')"
export MQTT_USER="$(bashio::services mqtt 'username')"
export MQTT_PASS="$(bashio::services mqtt 'password')"
export WAVPACK_LIB="$(ls /usr/lib/libwavpack.so* 2>/dev/null | head -n 1)"
export AIRPLAY="$(bashio::config 'airplay')"

if [ "$AIRPLAY" = "true" ]; then
    IFACE="$(ip route get "$SPEAKER_IP" | sed -n 's/.* dev \([^ ]*\).*/\1/p')"
    sed -e "s/@IFACE@/${IFACE}/" /app/avahi-daemon.conf > /etc/avahi/avahi-daemon.conf
    sed -e "s/@NAME@/${SPEAKER_NAME}/" -e "s/@OFFSET@/$(bashio::config 'airplay_offset')/" /app/shairport-sync.conf > /etc/shairport-sync.conf
    mkdir -p /run/dbus /var/run/avahi-daemon
    rm -f /run/dbus/dbus.pid /run/dbus/pid /var/run/avahi-daemon/pid
    dbus-uuidgen --ensure
    dbus-daemon --system --fork
    avahi-daemon --daemonize --no-chroot
    [ -p /tmp/shairport-sync-metadata ] || mkfifo /tmp/shairport-sync-metadata
    bashio::log.info "AirPlay op ${IFACE}"
fi

cd /app
exec python3 -u omni_addon.py
