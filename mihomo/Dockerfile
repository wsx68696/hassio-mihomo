ARG BUILD_FROM=python:3.13-alpine
FROM ${BUILD_FROM}
ENV PYTHONDONTWRITEBYTECODE=1
RUN apk add --no-cache iproute2 nftables ca-certificates
RUN pip install --no-cache-dir PyYAML==6.0.3
COPY vendor/mihomo /usr/local/bin/mihomo
COPY vendor/ui /opt/ha-mihomo/ui
COPY vendor/geodata /opt/ha-mihomo/geodata
COPY vendor/versions.json /opt/ha-mihomo/versions.json
COPY config.json /opt/ha-mihomo/addon.json
COPY licenses /opt/ha-mihomo/licenses
COPY config.py run.py updates.py downloads.py networking.py policies.py geodata.py setup.html /opt/ha-mihomo/
RUN chmod 0755 /usr/local/bin/mihomo
ARG BUILD_VERSION
LABEL io.hass.type="addon" io.hass.arch="amd64" \
      io.hass.name="Mihomo + Zashboard" io.hass.version="${BUILD_VERSION}" \
      org.opencontainers.image.source="https://github.com/wsx68696/hassio-mihomo" \
      org.opencontainers.image.licenses="GPL-3.0-only AND MIT"
ENTRYPOINT ["/usr/local/bin/python3", "-u", "/opt/ha-mihomo/run.py"]
