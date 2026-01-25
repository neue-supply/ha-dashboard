ARG BUILD_FROM
FROM $BUILD_FROM

# Install nginx
RUN apk add --no-cache nginx

# Copy configuration template
COPY nginx.conf.template /etc/nginx/nginx.conf.template
COPY run.sh /run.sh

# Make run script executable
RUN chmod +x /run.sh

# Expose ingress port
EXPOSE 8099

CMD ["/run.sh"]
