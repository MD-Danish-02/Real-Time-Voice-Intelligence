# Plivo Firewall Requirements - India (Mumbai) Region

**Source**: Official Plivo documentation (https://plivo.com/docs/voice/concepts/firewall-network-configuration/)

---

## 1. SIP Signaling (Inbound INVITE → Asterisk UDP 5060)
**Used in**: `pjsip.conf` → `[plivo-trunk-identify]` → `match=`

| CIDR | Purpose |
|------|---------|
| `13.52.9.4/32` | External SIP endpoint |
| `13.52.9.5/32` | External SIP endpoint |
| `18.214.109.132/32` | External SIP endpoint |
| `18.214.109.133/32` | External SIP endpoint |
| `15.207.90.192/31` | Zentrunk SIP Trunking (Mumbai) |
| `204.89.151.128/27` | Zentrunk SIP Trunking (Mumbai) |
| `204.89.151.160/27` | Zentrunk SIP Trunking (Mumbai) |

**Ports**: UDP 5060, TCP 5060/5061/5063/5080/443

---

## 2. RTP Media (Plivo → Asterisk UDP 10000-20000)
**Used in**: Firewall only (NOT in `identify` match)

| CIDR | Purpose |
|------|---------|
| `204.89.151.64/26` | Mumbai Voice API RTP media |
| `204.89.151.128/25` | Mumbai Voice API RTP media |

**Ports**: UDP 10000-30000 (Plivo uses 16384-32768; Asterisk `rtp.conf` uses 10000-20000)

---

## 3. Webhook Callbacks (Plivo → FastAPI HTTPS 443/8001)
**Used in**: Firewall on webhook endpoint server (NOT in Asterisk)

| CIDR | Purpose |
|------|---------|
| `3.7.4.233/32` | Mumbai callback |
| `3.7.220.174/32` | Mumbai callback |

**Ports**: TCP 443 (HTTPS) or 8001 (FastAPI direct)

---

## 4. Outbound API (FastAPI → Plivo REST API)
**Used in**: FastAPI egress firewall (if restrictive)

| Domain | Purpose |
|--------|---------|
| `api.plivo.com` | REST API (Voice, Numbers, Account) |
| `client.plivo.com` | SDK/WebSocket |

**Ports**: TCP 443 (HTTPS)

---

## 5. India Compliance (TRAI)
- **Oracle server (161.118.180.20) MUST be in Mumbai region (ap-mumbai-1)**
- All SIP signaling and RTP media for Indian calls must terminate in India
- Plivo account MUST be created with India data region
- Indian phone numbers only available to India data region accounts

---

## 6. Deployment Topology - Network Paths

```
┌─────────────┐     UDP 5060      ┌──────────────────┐
│   Plivo     │ ────────────────► │  Asterisk (bare- │
│  (Mumbai)   │  SIP Signaling    │  metal, 161.118. │
└─────────────┘                   │  180.20:5060)    │
                                  └────────┬─────────┘
                                           │ ARI (TCP 8088)
                                           ▼
                                  ┌──────────────────┐
                                  │  FastAPI Backend │
                                  │  (Docker, same   │
                                  │   host or diff)  │
                                  └────────┬─────────┘
                                           │ externalMedia
                                           ▼
                                  ┌──────────────────┐
                                  │  RTP Bridge UDP  │
                                  │  10000-10020     │
                                  └──────────────────┘
```

**Critical**: Bare-metal Asterisk cannot resolve Docker DNS `backend`.
- Set `VOICE_MEDIA_HOST_EXTERNAL=161.118.180.20` (or public hostname) in `.env`
- FastAPI binds RTP bridge to `0.0.0.0:10000-10020`
- Asterisk `externalMedia` connects to `VOICE_MEDIA_HOST_EXTERNAL:port`

---

## 7. Firewall Rules Summary (Oracle Cloud / iptables / ufw)

```bash
# SIP Signaling (Plivo → Asterisk)
iptables -A INPUT -p udp -s 13.52.9.4/32 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 13.52.9.5/32 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 18.214.109.132/32 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 18.214.109.133/32 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 15.207.90.192/31 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 204.89.151.128/27 --dport 5060 -j ACCEPT
iptables -A INPUT -p udp -s 204.89.151.160/27 --dport 5060 -j ACCEPT

# RTP Media (Plivo → Asterisk)
iptables -A INPUT -p udp -s 204.89.151.64/26 --dport 10000:20000 -j ACCEPT
iptables -A INPUT -p udp -s 204.89.151.128/25 --dport 10000:20000 -j ACCEPT

# RTP Bridge (Asterisk → FastAPI) - if on different hosts
iptables -A INPUT -p udp -s 161.118.180.20 --dport 10000:10020 -j ACCEPT

# Webhook Callbacks (Plivo → FastAPI)
iptables -A INPUT -p tcp -s 3.7.4.233/32 --dport 8001 -j ACCEPT
iptables -A INPUT -p tcp -s 3.7.220.174/32 --dport 8001 -j ACCEPT

# ARI (FastAPI → Asterisk)
iptables -A INPUT -p tcp -s 161.118.180.20 --dport 8088 -j ACCEPT

# Outbound Plivo API (FastAPI → api.plivo.com)
iptables -A OUTPUT -p tcp -d api.plivo.com --dport 443 -j ACCEPT
```

---

## 8. Verification Commands

```bash
# 1. Verify Asterisk identifies Plivo correctly
sudo asterisk -rx "pjsip show endpoint plivo-trunk-endpoint"

# 2. Verify dialplan match
sudo asterisk -rx "dialplan show from-trunk"

# 3. Test ARI connectivity from FastAPI container
curl -u voxai:PASSWORD http://161.118.180.20:8088/ari/applications

# 4. Verify RTP bridge port allocation
ss -ulnp | grep -E '1000[0-9]|1001[0-9]|10020'

# 5. Test externalMedia path (from Asterisk host)
# After call starts, check Asterisk logs for "external_media" channel creation

# 6. Verify Oracle region
curl -s http://169.254.169.254/opc/v2/instance/region
# Should return: ap-mumbai-1
```