# VoxAI Phase 3F.3 - Final Deployment Checklist

## ✅ Configuration Changes (Repository Only)

### 1. `apps/api/app/core/config.py`
- Renamed `PLIVO_SIP_DOMAIN` → `SIP_PUBLIC_HOST` (public hostname/IP of Asterisk PBX)
- Added `VOICE_MEDIA_HOST_EXTERNAL` for bare-metal Asterisk to reach FastAPI backend

### 2. `.env.example`
- `PLIVO_SIP_DOMAIN=` → `SIP_PUBLIC_HOST=`
- Added `VOICE_MEDIA_HOST_EXTERNAL=` (e.g., `161.118.180.20`)

### 3. `apps/api/app/api/v1/plivo.py`
- Uses `SIP_PUBLIC_HOST` for SIP URI generation: `sip:voxai_voice_app@{SIP_PUBLIC_HOST}`
- Log key: `sip_public_host_not_configured`

### 4. `infrastructure/asterisk/pjsip.conf`
- **Plivo endpoint/aor/identify** with unique section names
- **Identify match**: Official Plivo India (Mumbai) SIP signaling CIDRs only (7 entries)
- **Explicitly excluded**: RTP media IPs and callback IPs (firewall only)

### 5. `infrastructure/asterisk/extensions.conf`
- Explicit `exten => voxai_voice_app` before numeric patterns
- Ensures `${EXTEN}=voxai_voice_app` passed to Stasis

### 6. `apps/api/app/telephony/asterisk/event_handler.py`
- Uses `VOICE_MEDIA_HOST_EXTERNAL` (fallback to `VOICE_MEDIA_HOST`) for externalMedia

### 7. `infrastructure/asterisk/FIREWALL_REQUIREMENTS.md` (new)
- Documents all three IP categories with CIDRs and ports

---

## 🔴 Remaining Blockers (Must Resolve Before Deployment)

### 1. Oracle Server Region Verification
- **Action**: `curl -s http://169.254.169.254/opc/v2/instance/region`
- **Must return**: `ap-mumbai-1`
- **Why**: TRAI requires SIP+media anchoring in India for Indian calls

### 2. Plivo Account India Data Region
- **Verify**: Plivo account created with India data region (cannot change after signup)
- **Verify**: Indian numbers rented + KYC verified

### 3. `SIP_PUBLIC_HOST` Value
- **Set in production `.env`**: `SIP_PUBLIC_HOST=161.118.180.20` (or `pbx.example.com`)
- **Must match**: Asterisk `transport-udp` bind address (0.0.0.0:5060)

### 4. `VOICE_MEDIA_HOST_EXTERNAL` Value
- **Set in production `.env`**: `VOICE_MEDIA_HOST_EXTERNAL=161.118.180.20`
- **Why**: Bare-metal Asterisk cannot resolve Docker DNS `backend`

### 5. Firewall Rules Applied (Oracle Cloud Security Lists / iptables)
- [ ] SIP Signaling (7 CIDRs → UDP 5060)
- [ ] RTP Media (2 CIDRs → UDP 10000-20000)
- [ ] Webhook Callbacks (2 CIDRs → TCP 8001/443)
- [ ] ARI (FastAPI host → TCP 8088)
- [ ] Outbound Plivo API (FastAPI → `api.plivo.com`:443)

### 6. Asterisk RTP Port Range Alignment
- **Asterisk `rtp.conf`**: `rtpstart=10000`, `rtpend=20000` ✓ (already set)
- **FastAPI `VOICE_RTP_PORT_START/END`**: 10000-10020 ✓
- **Docker Compose**: `10000-10020:10000-10020/udp` on both `backend` and `asterisk` services ✓

### 7. ARI Credentials
- **Set in production `.env`**: `ARI_USERNAME=voxai`, `ARI_PASSWORD=<strong>`
- **Verify**: `http://161.118.180.20:8088/ari/` accessible from FastAPI host

### 8. TLS/HTTPS for Webhooks
- **Production requires**: Valid TLS cert for webhook URLs
- **Plivo requires**: HTTPS for `answer_url`/`hangup_url` in production

---

## 🟡 Verification Commands (Run After Config Deploy)

```bash
# 1. Reload Asterisk config (bare-metal)
sudo asterisk -rx "pjsip reload"
sudo asterisk -rx "dialplan reload"

# 2. Verify Plivo endpoint identified
sudo asterisk -rx "pjsip show endpoint plivo-trunk-endpoint"
# Should show: Identify: plivo-trunk-identify (match=...)

# 3. Verify dialplan
sudo asterisk -rx "dialplan show from-trunk"
# Should show: exten => voxai_voice_app,1,NoOp...

# 3. Test ARI from FastAPI host
curl -u voxai:$ARI_PASSWORD http://161.118.180.20:8088/ari/applications
# Should list: voxai_voice_app

# 4. Verify RTP bridge ports
ss -ulnp | grep -E '1000[0-9]|1001[0-9]|10020'
# Should show: python listening on 0.0.0.0:10000-10020

# 5. Test SIP connectivity (from Plivo console or sipvicious)
# Not required for config validation
```

---

## 🟢 Ready-for-Deployment Criteria

All of the above blockers resolved AND:
- [ ] Configuration committed to repository
- [ ] Production `.env` populated with real values (no `CHANGE_ME`)
- [ ] Firewall rules active in Oracle Cloud Console
- [ ] Asterisk reloaded without errors
- [ ] ARI connectivity verified from FastAPI host
- [ ] RTP bridge ports listening on backend
- [ ] Oracle region confirmed `ap-mumbai-1`

---

## 📋 Minimal Configuration Diff (Summary)

```diff
# apps/api/app/core/config.py
- PLIVO_SIP_DOMAIN: str = ""
+ SIP_PUBLIC_HOST: str = ""
+ VOICE_MEDIA_HOST_EXTERNAL: str = ""

# .env.example
- PLIVO_SIP_DOMAIN=
+ SIP_PUBLIC_HOST=
+ VOICE_MEDIA_HOST_EXTERNAL=

# apps/api/app/api/v1/plivo.py
- sip_domain = getattr(settings, "PLIVO_SIP_DOMAIN", None)
+ sip_host = getattr(settings, "SIP_PUBLIC_HOST", None)
- sip_uri = f"sip:voxai_voice_app@{sip_domain}"
+ sip_uri = f"sip:voxai_voice_app@{sip_host}"

# infrastructure/asterisk/pjsip.conf
+ [plivo-trunk-endpoint] ... (endpoint + aor)
+ [plivo-trunk-identify] match=13.52.9.4/32,13.52.9.5/32,18.214.109.132/32,18.214.109.133/32,15.207.90.192/31,204.89.151.128/27,204.89.151.160/27

# infrastructure/asterisk/extensions.conf
+ exten => voxai_voice_app,1,NoOp(Inbound VoxAI SIP call...)

# apps/api/app/telephony/asterisk/event_handler.py
+ media_host = settings.VOICE_MEDIA_HOST_EXTERNAL or settings.VOICE_MEDIA_HOST
+ ext_host = f"{media_host}:{media_session.local_port}"
```

---

## ⚠️ Do Not Deploy Until:
1. Oracle region = `ap-mumbai-1` confirmed
2. Plivo account = India data region confirmed
3. Both `SIP_PUBLIC_HOST` and `VOICE_MEDIA_HOST_EXTERNAL` set in production `.env`
4. All 11 firewall CIDR rules active
5. Asterisk reloads cleanly
6. ARI reachable from FastAPI host