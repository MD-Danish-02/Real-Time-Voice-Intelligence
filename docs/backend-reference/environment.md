# Environment
See `.env.example` (every variable, no real secrets). Key switches: `*_PROVIDER_MODE` (mock vs live per provider), `ENV`, `ALLOW_DEMO_SEED`, `KNOWLEDGE_EMBEDDING_MODE`. In `ENV=production` the API asserts: JWT secret changed, demo seed off, no mock provider. Secrets are never logged (structlog scrubber) and CRM tokens are Fernet-encrypted at rest.
