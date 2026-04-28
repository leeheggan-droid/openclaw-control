import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    ssh_host: str = os.getenv("OPENCLAW_SSH_HOST", "")
    # Read-only SSH lane — separate restricted user/key + forced-command wrapper
    # (bin/vibe-readonly-wrapper.sh) for probes, snapshots, autopilot evidence,
    # terminal pills, and /ops/report.  When unset, read-only features are
    # disabled gracefully; they NEVER fall back to ssh_host.
    ssh_readonly_host: str = os.getenv("OPENCLAW_SSH_READONLY_HOST", "")
    repo_dir: str = os.getenv("OPENCLAW_REPO_DIR", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_repo: str = os.getenv("GITHUB_REPO", "leeheggan-droid/openclaw-control")
    vibe_workdir: str = os.getenv("OPENCLAW_VIBE_WORKDIR", "")
    autopilot_interval: int = int(os.getenv("OPENCLAW_AUTOPILOT_INTERVAL", "300"))
    # Cheap Chat inference providers
    groq_api_key: str = os.getenv("GROQ_API_KEY", "")
    mistral_api_key: str = os.getenv("MISTRAL_API_KEY", "")
    cerebras_api_key: str = os.getenv("CEREBRAS_API_KEY", "")
    # Web search
    brave_api_key: str = os.getenv("BRAVE_API_KEY", "")
    # Trade log / inactivity alerting
    trade_inactivity_hours: int = int(os.getenv("OPENCLAW_TRADE_INACTIVITY_HOURS", "12"))
    alert_webhook_url: str = os.getenv("OPENCLAW_ALERT_WEBHOOK_URL", "")
    # Exchange API credentials (read-only trade history)
    kraken_api_key: str = os.getenv("KRAKEN_API_KEY", "")
    kraken_api_secret: str = os.getenv("KRAKEN_API_SECRET", "")
    alpaca_api_key: str = os.getenv("ALPACA_API_KEY", os.getenv("APCA_API_KEY_ID", ""))
    alpaca_api_secret: str = os.getenv("ALPACA_API_SECRET", os.getenv("APCA_API_SECRET_KEY", ""))
    # LLM provider selection for the VPS agent (anthropic | gemini | groq)
    llm_provider: str = os.getenv("LLM_PROVIDER", "anthropic")
    # Anthropic (Claude) — used when LLM_PROVIDER=anthropic
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-3-haiku-20240307")
    # Google Gemini — used when LLM_PROVIDER=gemini
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "")
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
    # Vibe executor (local HTTP gateway or CLI; used by the VPS agent)
    vibe_api_base: str = os.getenv("VIBE_API_BASE", "")
    vibe_api_key: str = os.getenv("VIBE_API_KEY", "")
    vibe_allowlist: str = os.getenv("VIBE_ALLOWLIST", "")
    # VPS agent polling interval (seconds)
    poll_interval_seconds: int = int(os.getenv("POLL_INTERVAL_SECONDS", "30"))

settings = Settings()