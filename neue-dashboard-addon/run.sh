#!/usr/bin/with-contenv bashio
set -e

# Read options from Home Assistant addon config
DOCUMENT_ROOT=$(bashio::config 'document_root')
MQTT_SERVER=$(bashio::config 'mqtt_server')

echo "Starting Neue Dashboard"
echo "Document root: ${DOCUMENT_ROOT}"
echo "MQTT server: ${MQTT_SERVER}"

# Start with the template
cp /etc/nginx/nginx.conf.template /etc/nginx/nginx.conf

# Replace document root
sed -i "s|__DOCUMENT_ROOT__|${DOCUMENT_ROOT}|g" /etc/nginx/nginx.conf

# Replace MQTT server
sed -i "s|__MQTT_SERVER__|${MQTT_SERVER}|g" /etc/nginx/nginx.conf

# Generate WLED proxy locations
WLED_LOCATIONS=""
for device in $(bashio::config 'wled_devices|keys'); do
    NAME=$(bashio::config "wled_devices[${device}].name")
    URL=$(bashio::config "wled_devices[${device}].url")

    echo "WLED device: ${NAME} -> ${URL}"

    WLED_LOCATIONS="${WLED_LOCATIONS}
        # WLED proxy: ${NAME}
        location /wled/${NAME} {
            proxy_pass ${URL};
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
done

# Insert WLED locations into nginx config
# Use awk to replace the placeholder since sed has issues with multiline
awk -v wled="$WLED_LOCATIONS" '{gsub(/__WLED_LOCATIONS__/, wled); print}' /etc/nginx/nginx.conf > /tmp/nginx.conf.tmp
mv /tmp/nginx.conf.tmp /etc/nginx/nginx.conf

echo "Generated nginx config:"
cat /etc/nginx/nginx.conf

# Start nginx in foreground
exec nginx -g "daemon off;"
