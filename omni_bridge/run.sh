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

cd /app
exec python3 -u omni_addon.py
