from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    anthropic_api_key: str = ""
    plivo_auth_id: str = ""
    plivo_auth_token: str = ""
    plivo_number: str = ""
    db_url: str = "mysql+pymysql://recsys:recsys2026@localhost/recsys"
    base_path: str = "/recsys"
    secret_key: str = "recsys-hostbuddy-2026"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    deepgram_api_key: str = ""

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings():
    return Settings()
