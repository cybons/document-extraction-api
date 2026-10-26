from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Client(BaseModel):
    key: SecretStr
    tasks: list[str]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DEX_", env_file=".env", extra="ignore")
    anthropic_api_key: SecretStr
    model: str = Field(min_length=1)
    clients: dict[str, Client]
    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    port: int = Field(default=8001, ge=1, le=65535)
    max_file_bytes: int = Field(default=20 * 1024 * 1024, ge=1)
    max_pages: int = Field(default=10, ge=1, le=10)
    max_pending: int = Field(default=100, ge=1)
    upload_seconds: float = Field(default=60, gt=0)
    preprocess_seconds: float = Field(default=60, gt=0)
    request_seconds: float = Field(default=120, gt=0)
    job_seconds: float = Field(default=600, gt=0)
    max_attempts: int = Field(default=3, ge=1, le=5)
    max_tokens: int = Field(default=8192, ge=1024, le=32768)
    result_days: int = Field(default=7, ge=1)
    history_days: int = Field(default=90, ge=1)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def check_clients(self):
        if not self.clients:
            raise ValueError("at least one client is required")
        keys = [c.key.get_secret_value() for c in self.clients.values()]
        if any(len(k) < 32 for k in keys) or len(keys) != len(set(keys)):
            raise ValueError("client keys must be unique and at least 32 characters")
        if self.history_days < self.result_days:
            raise ValueError("history_days must be >= result_days")
        api_key = self.anthropic_api_key.get_secret_value()
        if not api_key or api_key.startswith("replace-"):
            raise ValueError("configure an actual API key")
        if any(k.startswith("replace-") for k in keys):
            raise ValueError("configure client keys")
        if self.model.startswith("replace-"):
            raise ValueError("configure an actual model ID")
        return self
