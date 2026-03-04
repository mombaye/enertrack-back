#!/bin/bash
# docker/vpn/healthcheck.sh
# Vérifie que le serveur eFMS SQL Server est joignable

EFMS_HOST="${EFMS_SQL_HOST:-172.30.0.149}"
EFMS_PORT="${EFMS_SQL_PORT:-1433}"

# Test TCP sur le port SQL Server (1433)
if timeout 5 bash -c "echo > /dev/tcp/${EFMS_HOST}/${EFMS_PORT}" 2>/dev/null; then
    exit 0  # healthy
else
    exit 1  # unhealthy
fi