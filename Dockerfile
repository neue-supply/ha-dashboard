ARG BUILD_FROM
FROM $BUILD_FROM

# Install nginx + python3 (python3 for the config sync server)
RUN apk add --no-cache nginx python3

# Copy configuration template + server
COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY config-server.py /config-server.py
COPY run.sh /run.sh

# Make run script executable
RUN chmod +x /run.sh

# Expose ingress port
EXPOSE 8099

CMD ["/run.sh"]
