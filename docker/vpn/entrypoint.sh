#!/bin/bash
# docker/vpn/entrypoint.sh

set -e

VPNCMD="/opt/vpnclient/vpncmd"
LOG_PREFIX="[VPN-SIDECAR]"

log() { echo "${LOG_PREFIX} $1"; }

# ── 1. Démarrage du service VPN Client ──────────────────────────
log "Démarrage du service vpnclient..."
/opt/vpnclient/vpnclient start
sleep 3

# ── 2. Configuration de la connexion ────────────────────────────
# Paramètres (depuis .env) :
#   EFMS_VPN_HOST=185.120.147.180
#   EFMS_VPN_PORT=5555
#   EFMS_VPN_HUB=PowerBI-users
#   EFMS_VPN_USER=mmbaye
log "Configuration vers ${EFMS_VPN_HOST}:${EFMS_VPN_PORT} hub=${EFMS_VPN_HUB}..."

${VPNCMD} localhost /CLIENT /CMD \
    AccountCreate EnerTrack \
        /SERVER:"${EFMS_VPN_HOST}:${EFMS_VPN_PORT}" \
        /HUB:"${EFMS_VPN_HUB}" \
        /USERNAME:"${EFMS_VPN_USER}" \
        /NICNAME:vpn \
    2>/dev/null || log "Compte déjà existant, on continue."

${VPNCMD} localhost /CLIENT /CMD \
    AccountPasswordSet EnerTrack \
        /PASSWORD:"${EFMS_VPN_PASSWORD}" \
        /TYPE:standard \
    2>/dev/null || true

log "Connexion au VPN..."
${VPNCMD} localhost /CLIENT /CMD AccountConnect EnerTrack

# ── 3. Attente connexion effective ──────────────────────────────
MAX_WAIT=60
ELAPSED=0
until ${VPNCMD} localhost /CLIENT /CMD AccountStatusGet EnerTrack \
        2>/dev/null | grep -q "Session Status.*Connected"; do
    if [ $ELAPSED -ge $MAX_WAIT ]; then
        log "ERREUR: VPN non connecté après ${MAX_WAIT}s"
        exit 1
    fi
    log "Attente connexion VPN... (${ELAPSED}s)"
    sleep 5
    ELAPSED=$((ELAPSED + 5))
done
log "VPN connecté."

# ── 4. DHCP sur l'interface VPN ──────────────────────────────────
log "DHCP sur vpn_vpn..."
udhcpc -i vpn_vpn -q 2>/dev/null || true
sleep 2

# ── 5. Route vers réseau eFMS ────────────────────────────────────
IFACE=$(ip link | grep vpn | awk -F': ' '{print $2}' | head -1)
if [ -n "$IFACE" ]; then
    ip route add "${EFMS_NETWORK:-172.30.0.0/24}" dev "$IFACE" 2>/dev/null || true
    log "Route ajoutée: ${EFMS_NETWORK:-172.30.0.0/24} via $IFACE"
fi

log "VPN sidecar prêt."

# ── 6. Keepalive + reconnexion automatique ───────────────────────
# Reconnect Interval: 15s (comme configuré dans SoftEther)
while true; do
    sleep 15
    STATUS=$(${VPNCMD} localhost /CLIENT /CMD AccountStatusGet EnerTrack \
             2>/dev/null | grep "Session Status" || echo "DISCONNECTED")
    if ! echo "$STATUS" | grep -q "Connected"; then
        log "Déconnecté, reconnexion..."
        ${VPNCMD} localhost /CLIENT /CMD AccountConnect EnerTrack 2>/dev/null || true
    fi
done