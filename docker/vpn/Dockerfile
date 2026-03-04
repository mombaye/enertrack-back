# docker/vpn/Dockerfile — VERSION FINALE

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
        libssl-dev \
        libreadline-dev \
        zlib1g-dev \
        iproute2 \
        iputils-ping \
        iptables \
        isc-dhcp-client \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Build depuis les sources — make à la RACINE (pas dans un sous-dossier)
RUN git clone --depth 1 --branch master \
        https://github.com/SoftEtherVPN/SoftEtherVPN_Stable.git \
        /tmp/softether \
    && cd /tmp/softether \
    && yes 1 | make \
    && mkdir -p /opt/vpnclient \
    && cp vpnclient /opt/vpnclient/vpnclient \
    && cp vpncmd    /opt/vpnclient/vpncmd \
    && cp hamcore.se2 /opt/vpnclient/hamcore.se2 \
    && rm -rf /tmp/softether

WORKDIR /opt/vpnclient

COPY docker/vpn/entrypoint.sh /entrypoint.sh
COPY docker/vpn/healthcheck.sh /healthcheck.sh
RUN chmod +x /entrypoint.sh /healthcheck.sh

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD /healthcheck.sh

ENTRYPOINT ["/entrypoint.sh"]