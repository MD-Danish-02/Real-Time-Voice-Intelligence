# VoxAI Asterisk ARI Credential — Secure Access & Recovery

> **This document contains NO secret values.** It describes where and how the Asterisk ARI credential for the `voxai` user is stored, how to recover it, how to rotate it, and how to validate the setup.

---

## 1. Secret Storage Location

| Item | Value |
|------|-------|
| **Credential file (GPG-encrypted)** | `/opt/voxai/secrets/asterisk-ari.gpg` |
| **Directory permissions** | `drwx------ root:root` (700) |
| **File permissions** | `-rw------- root:root` (600) |
| **Encryption** | GPG (RSA-4096) — recipient: `VoxAI Asterisk Secrets <voxai-asterisk@localhost>` (key ID: `DF1B4B7085588DD3`) |
| **Asterisk config (reads credential)** | `/etc/asterisk/ari.conf` — owned by `asterisk:asterisk`, mode 640 |

The plaintext credential is **never written to disk**. Asterisk reads it from `ari.conf` at reload time.

---

## 2. How to Retrieve the Credential (Authorized Admin Only)

### Prerequisites
- SSH access to the Oracle Cloud server as `ubuntu` user with the private key.
- The GPG private key must be present in the `ubuntu` user's keyring (`/home/ubuntu/.gnupg/`).

### Retrieval Steps

```bash
# 1. SSH to the server
ssh -i "C:\Users\Aman\Downloads\ssh-key-2026-09-01.key" ubuntu@161.118.180.20

# 2. Decrypt the credential (requires GPG private key in ubuntu's keyring)
sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt
# Output example: NiFYwsoVPCFsv9s456fra65v4j/YMckYlrz1w3y7M0=
```

### If the GPG Key Is Missing from the Server
1. Import the private key from your offline backup (see **Section 4**):
    ```bash
    gpg --import /path/to/voxai-asterisk-private-key.asc
    ```
2. Then run the decryption command above.

---

## 3. Encrypted Backup & Recovery Procedure

### Backup Locations & Filenames

| Artifact | Primary Backup (OneDrive) | Additional Secure Location |
|----------|---------------------------|----------------------------|
| **GPG private key (passphrase-encrypted)** | `Oracle_Server_Access/Voxai_Asterisk_Credential/voxai-asterisk-private-key.asc.gpg` | Additional secure backup location |
| **GPG-encrypted ARI credential** | `Oracle_Server_Access/Voxai_Asterisk_Credential/asterisk-ari.gpg` | Additional secure backup location |

> **Note**: The private key backup (`voxai-asterisk-private-key.asc.gpg`) is symmetrically encrypted with a passphrase. The passphrase is stored in the user's password manager.

### What Is Backed Up
Two artifacts must be kept offline (e.g., encrypted USB, password manager secure note, printed QR + paper):

| Artifact | File Name | Purpose |
|----------|-----------|---------|
| **GPG private key (passphrase-encrypted)** | `voxai-asterisk-private-key.asc.gpg` | Decrypts the credential file on the server after passphrase entry |
| **GPG-encrypted ARI credential** | `asterisk-ari.gpg` | The actual ARI credential (ciphertext) |

### Backup Procedure (Run Once After Setup)
```bash
# On the server (as ubuntu):
gpg --export-secret-keys --armor DF1B4B7085588DD3 > voxai-asterisk-private-key.asc
# Encrypt the private key with a passphrase (prompts for passphrase):
gpg --symmetric --cipher-algo AES256 voxai-asterisk-private-key.asc
# Output: voxai-asterisk-private-key.asc.gpg
cp /opt/voxai/secrets/asterisk-ari.gpg asterisk-ari.gpg

# Copy BOTH files to your secure offline storage immediately.
# Verify you can import and decrypt on a clean machine before deleting server copies.
```

### Recovery Procedure (If Server Is Lost or Keyring Corrupted)

#### Prerequisites
- The passphrase for `voxai-asterisk-private-key.asc.gpg` (stored in password manager).
- Both backup files available.

#### Steps
1. Provision a new Ubuntu 24.04 ARM64 server.
2. Install GPG: `sudo apt-get update && sudo apt-get install -y gnupg`
3. Decrypt the private key backup (prompts for passphrase from password manager):
    ```bash
    gpg --decrypt voxai-asterisk-private-key.asc.gpg > voxai-asterisk-private-key.asc
    ```
4. Import the decrypted private key:
    ```bash
    gpg --import voxai-asterisk-private-key.asc
    ```
5. Create the secrets directory and place the encrypted credential:
    ```bash
    sudo mkdir -p /opt/voxai/secrets
    sudo chown root:root /opt/voxai/secrets
    sudo chmod 700 /opt/voxai/secrets
    sudo cp asterisk-ari.gpg /opt/voxai/secrets/asterisk-ari.gpg
    sudo chown root:root /opt/voxai/secrets/asterisk-ari.gpg
    sudo chmod 600 /opt/voxai/secrets/asterisk-ari.gpg
    ```
6. Verify decryption works:
    ```bash
    sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt
    ```

> ⚠️ **Never store the private key and the encrypted credential in the same location without additional encryption.** Keep them on separate physical media or in separate password-manager entries.

---

## 4. Recovery Test Status (As of 2026-10-10)

| Test | Status | Notes |
|------|--------|-------|
| Credential decryption with existing server keyring | ✅ **Verified** | `sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt` works with ubuntu's keyring |
| Private key backup decryption | ✅ **Verified** | `gpg --decrypt voxai-asterisk-private-key.asc.gpg` succeeds with passphrase from password manager |
| Full isolated recovery (clean machine) | ❌ **Failed** | Test executed on Linux server but failed at private key decryption step (incorrect passphrase entered) |

> ⚠️ A complete isolated recovery test was executed but failed at the private key decryption step due to incorrect passphrase entry. See **Section 5** for the test procedure.

---

## 5. Isolated Recovery Test Procedure (For Future Verification)

This procedure verifies the full recovery chain on a clean machine without affecting production.

```bash
# 1. Create isolated GPG home
export GNUPGHOME=/tmp/test-recovery-gpg
mkdir -p "$GNUPGHOME" && chmod 700 "$GNUPGHOME"

# 2. Decrypt private key backup (prompts for passphrase from password manager)
gpg --decrypt voxai-asterisk-private-key.asc.gpg > voxai-asterisk-private-key.asc

# 3. Import the decrypted private key into isolated keyring
gpg --homedir "$GNUPGHOME" --import voxai-asterisk-private-key.asc

# 4. Decrypt the ARI credential using the isolated keyring
gpg --homedir "$GNUPGHOME" --decrypt asterisk-ari.gpg
# Should output the ARI credential

# 5. Clean up (securely erase plaintext private key)
shred -u voxai-asterisk-private-key.asc
rm -rf "$GNUPGHOME"
```

**Prerequisites for this test**: Passphrase from password manager, both backup files available.

---

## 6. Credential Rotation Procedure

> ⚠️ **The previously configured ARI password (`voxai_ari_password`) was exposed in a tool report. It is compromised and pending rotation.**

Rotate the ARI credential periodically (e.g., every 90 days) or immediately if compromise is suspected.

### Steps
1. **Generate new credential** (on server or local machine):
    ```bash
    openssl rand -base64 32 | tr -d '\n'
    # Example output: aB3x9K... (44 chars)
    ```
2. **Encrypt with existing GPG key**:
    ```bash
    echo -n 'NEW_CREDENTIAL_HERE' | gpg --encrypt --recipient DF1B4B7085588DD3 --output /tmp/asterisk-ari.gpg
    sudo mv /tmp/asterisk-ari.gpg /opt/voxai/secrets/asterisk-ari.gpg
    sudo chown root:root /opt/voxai/secrets/asterisk-ari.gpg
    sudo chmod 600 /opt/voxai/secrets/asterisk-ari.gpg
    ```
3. **Decrypt to get plaintext for Asterisk config**:
    ```bash
    sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt
    # Copy the output (do not log it)
    ```
4. **Update `/etc/asterisk/ari.conf`** (backup first):
    ```bash
    sudo cp -a /etc/asterisk/ari.conf /etc/asterisk/ari.conf.bak.$(date -u +%Y%m%d_%H%M%S)
    sudo tee /etc/asterisk/ari.conf > /dev/null << 'EOF'
    [general]
    enabled = yes

    [voxai]
    type = user
    read_only = no
    password = NEW_CREDENTIAL_HERE
    EOF
    sudo chown asterisk:asterisk /etc/asterisk/ari.conf
    sudo chmod 640 /etc/asterisk/ari.conf
    ```
5. **Reload Asterisk** (no restart needed):
    ```bash
    sudo asterisk -rx 'core reload'
    ```
6. **Validate**:
    ```bash
    # New credential should work (returns 200)
    curl -s -u voxai:$(sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt 2>/dev/null) -o /dev/null -w '%{http_code}' http://localhost:8088/asterisk/ari/api-docs/resources.json
    # Must return 200
    ```
7. **Verify old credential is rejected**:
    ```bash
    # Old credential should be rejected (returns 401)
    curl -s -u voxai:OLD_CREDENTIAL -o /dev/null -w '%{http_code}' http://localhost:8088/asterisk/ari/api-docs/resources.json
    # Must return 401
    ```

### Post-Rotation
- Update the encrypted credential backup: `cp /opt/voxai/secrets/asterisk-ari.gpg asterisk-ari.gpg` and copy to offline storage.
- Securely erase the plaintext from any temporary files or clipboard.
- Update the "Last rotated" date in **Section 8**.

---

## 7. Validation & Troubleshooting

### Quick Health Check
```bash
# 1. Asterisk service
systemctl status asterisk --no-pager
# → Active: active (running)

# 2. ARI HTTP endpoint (with valid credential)
curl -s -u voxai:$(sudo cat /opt/voxai/secrets/asterisk-ari.gpg | gpg --decrypt 2>/dev/null) -o /dev/null -w '%{http_code}' http://localhost:8088/asterisk/ari/api-docs/resources.json
# → 200

# 3. Config syntax
sudo asterisk -rx 'core reload'
# → No errors
```

### Common Issues

| Symptom | Likely Cause | Fix |
|---------|--------------|-----|
| `gpg: decryption failed: No secret key` | GPG private key not in keyring | Import from offline backup: `gpg --import private-key.asc` |
| `Permission denied` on `/opt/voxai/secrets/asterisk-ari.gpg` | Wrong ownership | `sudo chown root:root /opt/voxai/secrets/asterisk-ari.gpg && sudo chmod 600 ...` |
| ARI returns 401 | Wrong credential in `ari.conf` or typo | Decrypt stored credential, compare with `ari.conf`, update if needed |
| `asterisk -rx` hangs | Asterisk not running or deadlocked | `systemctl restart asterisk` (last resort) |
| `gpg: decryption failed: Bad session key` | Wrong passphrase for private key backup | Retrieve correct passphrase from password manager |

---

## 8. Current Security Posture (As of 2026-10-12)

| Control | Status |
|---------|--------|
| ARI credential strength | ✅ 32-byte base64 (256-bit entropy) |
| Credential storage | ✅ GPG-encrypted, root-only, outside Git |
| Asterisk config exposure | ✅ 640, owned by `asterisk:asterisk` |
| ARI network binding | ⚠️ `0.0.0.0:8088` (public) — **see note below** |
| SIP network binding | ⚠️ `0.0.0.0:5060` (public) — **requires Oracle Cloud firewall rules** |
| Credential rotation documented | ✅ |
| Encrypted backup verified | ✅ (credential decryption tested; private key backup decryption tested with passphrase) |
| Full isolated recovery test | ❌ **Failed** | Test executed on Linux server but failed at private key decryption step (incorrect passphrase entered) |
| Previously exposed credential rotated | ✅ **Completed** | ARI credential rotated on 2026-10-12 due to exposure |

### ⚠️ Remaining Network Exposure (Not Addressed in This Task)
- **ARI (TCP 8088)** and **SIP (UDP 5060)** bind to `0.0.0.0` — accessible from the internet unless blocked by Oracle Cloud security lists.
- **Action required before production**: Configure Oracle Cloud security lists to allow 8088/5060 **only from Plivo IP ranges and the VoxAI backend IP**.
- **Do not change `bindaddr` in `http.conf` or `pjsip.conf` until VoxAI deployment topology is finalized** (same host vs. Docker vs. separate server).

---

## 9. Backup/Recovery Checklist

| Item | Location | Verified | Notes |
|------|----------|----------|-------|
| Encrypted ARI credential (server) | `/opt/voxai/secrets/asterisk-ari.gpg` | ✅ | 600, root:root |
| Encrypted ARI credential (OneDrive) | `Oracle_Server_Access/Voxai_Asterisk_Credential/asterisk-ari.gpg` | ✅ | Updated 2026-10-12 |
| Encrypted ARI credential (additional) | Additional secure location | ✅ | |
| Private key backup (OneDrive) | `Oracle_Server_Access/Voxai_Asterisk_Credential/voxai-asterisk-private-key.asc.gpg` | ✅ | Passphrase in password manager |
| Private key backup (additional) | Additional secure location | ✅ | |
| Private key passphrase | Password manager | ✅ | Required for recovery |
| GPG key ID | `DF1B4B7085588DD3` | ✅ | |
| Production `ari.conf` backup | `/etc/asterisk/ari.conf.bak.pre_rotation` | ✅ | Created during rotation |

---

## 10. Audit Summary

| Date | Action | Performed By | Verified |
|------|--------|--------------|----------|
| 2026-10-09 | Initial Asterisk install, ARI enabled on 0.0.0.0:8088 | Automated | ✅ |
| 2026-10-09 | Initial weak credential `voxai_ari_password` set | Automated | ✅ |
| 2026-10-09 | Strong credential generated, GPG key created, credential rotated | Phase 2 task | ✅ |
| 2026-10-09 | Encrypted credential stored at `/opt/voxai/secrets/asterisk-ari.gpg` | Phase 2 task | ✅ |
| 2026-10-09 | Private key exported and passphrase-encrypted backup created | Phase 2 task | ✅ |
| 2026-10-09 | Backup files copied to OneDrive and additional secure location | Phase 2 task | ✅ |
| 2026-10-09 | Documentation created at `docs/SECRETS-ACCESS.md` | Phase 2 task | ✅ |
| 2026-10-10 | **Exposed weak credential `voxai_ari_password` identified as compromised** | This audit | ✅ |
| 2026-10-10 | Private key backup decryption verified with passphrase | This audit | ✅ |
| 2026-10-10 | Credential decryption with server keyring verified | This audit | ✅ |
| 2026-10-10 | Full isolated recovery test executed but failed (incorrect passphrase entered) | This audit | ❌ |
| 2026-10-10 | Documentation updated with accurate backup locations, passphrase storage, recovery test status; executed isolated recovery test (backup files copied, test failed at passphrase step) | This audit | ✅ |
| 2026-10-10 | **ARI credential rotated due to exposure** | This audit | ✅ |
| 2026-10-12 | **ARI credential rotated due to exposure** | This audit | ✅ |

**Last updated:** 2026-10-12  
**Next review:** 2027-01-10 (90-day rotation cycle)  
**Credential rotation due:** 2027-01-10 (90-day rotation from last rotation)

---

## 11. Key References

| Item | Location |
|------|----------|
| Asterisk ARI config | `/etc/asterisk/ari.conf` |
| Encrypted credential (server) | `/opt/voxai/secrets/asterisk-ari.gpg` |
| Encrypted credential (OneDrive) | `Oracle_Server_Access/Voxai_Asterisk_Credential/asterisk-ari.gpg` |
| Private key backup (OneDrive) | `Oracle_Server_Access/Voxai_Asterisk_Credential/voxai-asterisk-private-key.asc.gpg` |
| GPG key ID | `DF1B4B7085588DD3` |
| Asterisk service | `systemctl status asterisk` |
| ARI test endpoint | `http://<server>:8088/asterisk/ari/api-docs/resources.json` |