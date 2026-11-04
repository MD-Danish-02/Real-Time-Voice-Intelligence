"""
Central application settings. All values are loaded from environment
variables (see .env.example at the repo root). Nothing here should ever
contain a real secret - defaults are safe for local development only.
"""
from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Core ---
    APP_NAME: str = "VoxAI Voice Automation Platform"
    ENV: str = "development"  # development | staging | production
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"
    FRONTEND_URL: str = "http://localhost:3000"
    BACKEND_URL: str = "http://localhost:8001"

    # --- Database ---
    DATABASE_URL: str = "postgresql+psycopg2://voxai:voxai@postgres:5432/voxai"
    ASYNC_DATABASE_URL: str = "postgresql+asyncpg://voxai:voxai@postgres:5432/voxai"

    # --- Redis / Queue ---
    REDIS_URL: str = "redis://redis:6379/0"
    CELERY_BROKER_URL: str = "redis://redis:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://redis:6379/2"

    # --- Security ---
    JWT_SECRET: str = "CHANGE_ME_DEV_ONLY"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    AUTH_REFRESH_COOKIE_NAME: str = "voxai_refresh"
    AUTH_REFRESH_COOKIE_SECURE: bool = False  # set true behind HTTPS
    AUTH_REFRESH_COOKIE_SAMESITE: str = "lax"
    AUTH_REFRESH_COOKIE_DOMAIN: str = ""
    ENCRYPTION_KEY: str = "CHANGE_ME_32_BYTE_KEY_DEV_ONLY!!"
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 30

    # --- Email ---
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = ""
    SMTP_USE_STARTTLS: bool = True
    SMTP_TIMEOUT_SECONDS: int = 10

    # --- Rate limiting ---
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_API_PER_MINUTE: int = 120

    # --- Demo / seed ---
    ALLOW_DEMO_SEED: bool = True  # must be false in real production deploys

    # --- Provider mode ---
    # "mock" uses local, deterministic fake providers for development/testing.
    # "live" requires real credentials below.
    CRM_PROVIDER_MODE: str = "mock"
    TELEPHONY_PROVIDER_MODE: str = "mock"
    CALENDAR_PROVIDER_MODE: str = "mock"
    WHATSAPP_PROVIDER_MODE: str = "mock"
    KNOWLEDGE_EMBEDDING_MODE: str = "local"  # local (hashing, no ML deps) | sentence_transformers
    STT_PROVIDER_MODE: str = "mock"
    LLM_PROVIDER_MODE: str = "mock"
    TTS_PROVIDER_MODE: str = "mock"

    # --- LLM ---
    GROQ_API_KEY: str = ""
    GROQ_MODEL: str = "llama-3.3-70b-versatile"

    # --- STT / TTS ---
    FASTER_WHISPER_MODEL: str = "base"
    STT_LANGUAGE: str = "auto"  # auto | hi | en
    TTS_PROVIDER_NAME: str = "kokoro_onnx"
    TTS_API_KEY: str = ""
    CHATTERBOX_BASE_URL: str = ""
    CHATTERBOX_VOICE_ID: str = "riya"
    CHATTERBOX_DEVICE: str = "cpu"
    INDICF5_BASE_URL: str = ""
    KOKORO_MODEL_PATH: str = "models/kokoro/kokoro-v1.0.onnx"
    KOKORO_VOICES_PATH: str = "models/kokoro/voices-v1.0.bin"
    KOKORO_VOICE_ID: str = "hf_alpha"
    KOKORO_DEFAULT_LANG: str = "hi"
    KOKORO_SPEED: float = 1.0
    # kokoro-onnx create() accepts float speed in the inclusive [0.5, 2.0] range.
    KOKORO_HINDI_SPEED: float = 1.15
    KOKORO_ENGLISH_SPEED: float = 1.0

    # --- Asterisk / ARI / RTP ---
    ARI_BASE_URL: str = "http://asterisk:8088/ari"
    ARI_USERNAME: str = "voxai"
    ARI_PASSWORD: str = "CHANGE_ME"
    ARI_APP_NAME: str = "voxai_voice_app"
    VOICE_MEDIA_HOST: str = "backend"
    VOICE_MEDIA_HOST_EXTERNAL: str = ""  # Public IP/hostname for bare-metal Asterisk to reach backend
    VOICE_RTP_PORT_START: int = 10000
    VOICE_RTP_PORT_END: int = 10020

    # --- Telephony providers ---
    EXOTEL_SID: str = ""
    EXOTEL_API_KEY: str = ""
    EXOTEL_API_TOKEN: str = ""
    EXOTEL_SUBDOMAIN: str = "api.exotel.com"
    EXOTEL_CALLER_ID: str = ""
    PLIVO_AUTH_ID: str = ""
    PLIVO_AUTH_TOKEN: str = ""
    PLIVO_CALLER_ID: str = ""
    SIP_PUBLIC_HOST: str = ""
    GENERIC_SIP_HOST: str = ""
    GENERIC_SIP_USERNAME: str = ""
    GENERIC_SIP_PASSWORD: str = ""

    # --- CRM providers (OAuth app credentials) ---
    HUBSPOT_CLIENT_ID: str = ""
    HUBSPOT_CLIENT_SECRET: str = ""
    HUBSPOT_REDIRECT_URI: str = "http://localhost:8001/api/v1/crm/oauth/hubspot/callback"
    SALESFORCE_CLIENT_ID: str = ""
    SALESFORCE_CLIENT_SECRET: str = ""
    SALESFORCE_REDIRECT_URI: str = "http://localhost:8001/api/v1/crm/oauth/salesforce/callback"
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REDIRECT_URI: str = "http://localhost:8001/api/v1/crm/oauth/zoho/callback"
    PIPEDRIVE_CLIENT_ID: str = ""
    PIPEDRIVE_CLIENT_SECRET: str = ""
    PIPEDRIVE_REDIRECT_URI: str = "http://localhost:8001/api/v1/crm/oauth/pipedrive/callback"

    # --- Google Calendar ---
    GOOGLE_CLIENT_ID: str = ""
    GOOGLE_CLIENT_SECRET: str = ""
    GOOGLE_REDIRECT_URI: str = "http://localhost:8001/api/v1/integrations/google/callback"
    GOOGLE_SHEETS_REDIRECT_URI: str = "http://localhost:8001/api/v1/crm/oauth/google_sheets/callback"

    # --- WhatsApp ---
    WHATSAPP_PROVIDER: str = "meta_cloud_api"
    WHATSAPP_ACCESS_TOKEN: str = ""
    WHATSAPP_PHONE_NUMBER_ID: str = ""
    WHATSAPP_VERIFY_TOKEN: str = "CHANGE_ME"
    WHATSAPP_APP_SECRET: str = ""

    # --- Object storage (S3-compatible) ---
    S3_ENDPOINT_URL: str = ""
    S3_ACCESS_KEY: str = "voxai"
    S3_SECRET_KEY: str = "CHANGE_ME"
    S3_BUCKET: str = "voxai-media"
    S3_REGION: str = "us-east-1"

    CORS_ORIGINS: List[str] = ["http://localhost:3000", "http://100.80.145.109:3000"]


@lru_cache
def get_settings() -> "Settings":
    return Settings()


settings = get_settings()
