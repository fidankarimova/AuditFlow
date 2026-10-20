"""Runtime configuration loaded from environment variables / .env."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=REPO_ROOT / ".env", extra="ignore")

    # Ledger
    ledger_target: Literal["fisco", "evm", "none"] = "evm"
    rpc_url: str = "https://sepolia.base.org"
    chain_id: int = 84532
    admin_private_key: str = ""
    coordinator_private_key: str = ""

    # Human approvers (demo keys held server-side; documented known risk)
    supplier_signer_key: str = ""
    buyer_signer_key: str = ""

    # Bank sandbox (simulated bank, separate service)
    bank_webhook_secret: str = ""
    bank_url: str = "http://127.0.0.1:8300"
    bank_webhook_url: str = "http://127.0.0.1:8200/webhooks/bank"  # coordinator endpoint
    bank_step_seconds: float = 2.0  # delay between INITIATED -> ACCEPTED -> SETTLED
    bank_fail_rate: float = 0.0  # probability of an injected 503
    bank_admin_token: str = ""  # optional guard for /admin/* failure injection

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = ""
    llm_mode: Literal["offline", "online"] = "offline"
    # online provider: anthropic (Claude) or groq (OpenAI-compatible chat completions API)
    llm_provider: Literal["anthropic", "groq"] = "anthropic"
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"  # llama-3.3-70b-versatile is no longer served by Groq (model_not_found)
    groq_base_url: str = "https://api.groq.com/openai/v1"

    # Service URLs
    supplier_url: str = "http://127.0.0.1:8101"
    buyer_url: str = "http://127.0.0.1:8102"
    coordinator_url: str = "http://127.0.0.1:8200"

    # Optimizer
    allow_partial: bool = True


def get_settings() -> Settings:
    return Settings()
