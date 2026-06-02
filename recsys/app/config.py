from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    openai_api_key: str = ""       # primary LLM — gpt-4o-mini
    anthropic_api_key: str = ""    # kept for fallback / future use
    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_number: str = ""
    db_url: str = "mysql+pymysql://recsys:recsys2026@localhost/recsys"
    base_path: str = "/recsys"
    secret_key: str = "recsys-hostbuddy-2026"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    deepgram_api_key: str = ""           # kept for voice_web.py REST transcription
    google_credentials: str = ""         # path to service account JSON (set via GOOGLE_APPLICATION_CREDENTIALS env var)

    google_application_credentials: str = ""  # service account JSON path

    # PayPal — sandbox by default. Get keys at developer.paypal.com → Apps & Credentials.
    paypal_client_id: str = ""
    paypal_secret:    str = ""
    paypal_mode:      str = "sandbox"   # "sandbox" or "live"

    # Public origin used to build SMS/checkout links (no trailing slash).
    public_base_url:  str = "https://support.hostbuddy.io/recsys"

    # Email (SRM weekly campaigns). Any free SMTP works — e.g. Gmail app password,
    # Brevo, SendGrid SMTP. If unset, SRM "sends" are simulated (recorded only).
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""   # display From; defaults to smtp_user

    class Config:
        env_file = ".env"
        extra = "ignore"   # don't fail on unknown env vars


@lru_cache()
def get_settings():
    return Settings()
