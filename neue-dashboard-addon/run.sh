#!/usr/bin/with-contenv bashio
set -e

# Read options from Home Assistant addon config
DOCUMENT_ROOT=$(bashio::config 'document_root')

# Update nginx config with document root
sed -i "s|__DOCUMENT_ROOT__|${DOCUMENT_ROOT}|g" /etc/nginx/nginx.conf

echo "Starting Neue Dashboard"
echo "Document root: ${DOCUMENT_ROOT}"

# Start nginx in foreground
exec nginx -g "daemon off;"
