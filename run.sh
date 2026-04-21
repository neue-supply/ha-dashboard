#!/usr/bin/with-contenv bashio
set -e

# Read options from Home Assistant addon config
DOCUMENT_ROOT=$(bashio::config 'document_root')
MQTT_SERVER=$(bashio::config 'mqtt_server')
GO2RTC_URL=$(bashio::config 'go2rtc_url' '')
HA_URL=$(bashio::config 'ha_url' 'http://supervisor/core')

# Convert ws:// to http:// for nginx proxy_pass (WebSocket upgrade happens via headers)
MQTT_PROXY_URL=$(echo "$MQTT_SERVER" | sed 's|^ws://|http://|' | sed 's|^wss://|https://|')

echo "Starting Neue Dashboard"
echo "Document root: ${DOCUMENT_ROOT}"
echo "MQTT server: ${MQTT_SERVER} -> ${MQTT_PROXY_URL}"
echo "go2rtc URL: ${GO2RTC_URL}"
echo "HA URL: ${HA_URL}"

# Start with the template
cp /etc/nginx/nginx.conf.template /etc/nginx/nginx.conf

# Replace document root
sed -i "s|__DOCUMENT_ROOT__|${DOCUMENT_ROOT}|g" /etc/nginx/nginx.conf

# Replace MQTT server (use http:// URL for nginx)
sed -i "s|__MQTT_SERVER__|${MQTT_PROXY_URL}|g" /etc/nginx/nginx.conf

# Generate go2rtc proxy location if configured
GO2RTC_LOCATION=""
if [ -n "${GO2RTC_URL}" ]; then
    GO2RTC_LOCATION="
        # go2rtc camera streams proxy (^~ stops regex matching for .js/.css files)
        location ^~ /go2rtc/ {
            proxy_pass ${GO2RTC_URL}/;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_buffering off;
            proxy_read_timeout 86400;
            proxy_send_timeout 86400;
        }
"
fi

# Insert go2rtc location into nginx config
awk -v go2rtc="$GO2RTC_LOCATION" '{gsub(/__GO2RTC_LOCATION__/, go2rtc); print}' /etc/nginx/nginx.conf > /tmp/nginx.conf.tmp
mv /tmp/nginx.conf.tmp /etc/nginx/nginx.conf

# Generate HA API/WebSocket proxy location
SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN:-}"
HA_LOCATION="
        # Home Assistant token endpoint (for WebSocket auth)
        location = /ha/token {
            default_type text/plain;
            return 200 '${SUPERVISOR_TOKEN}';
        }

        # Home Assistant API proxy
        location /ha/api/ {
            proxy_pass ${HA_URL}/api/;
            proxy_http_version 1.1;
            proxy_set_header Authorization \"Bearer ${SUPERVISOR_TOKEN}\";
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
        }

        # Home Assistant WebSocket proxy
        location /ha/websocket {
            proxy_pass ${HA_URL}/api/websocket;
            proxy_http_version 1.1;
            proxy_set_header Upgrade \$http_upgrade;
            proxy_set_header Connection \$connection_upgrade;
            proxy_set_header Host \$host;
            proxy_set_header X-Real-IP \$remote_addr;
            proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto \$scheme;
            proxy_read_timeout 86400;
            proxy_send_timeout 86400;
        }
"

# Insert HA location into nginx config
awk -v ha="$HA_LOCATION" '{gsub(/__HA_LOCATION__/, ha); print}' /etc/nginx/nginx.conf > /tmp/nginx.conf.tmp
mv /tmp/nginx.conf.tmp /etc/nginx/nginx.conf

echo "Generated nginx config:"
cat /etc/nginx/nginx.conf

# Start the dashboard config sync server (Python, 127.0.0.1:8100) in the background.
# nginx proxies /api/config* here. Persistent storage lives at /data/dashboard-config.json.
python3 /config-server.py &

# Start nginx in foreground
exec nginx -g "daemon off;"
