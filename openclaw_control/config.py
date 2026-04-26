import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    ssh_host: str = os.getenv("OPENCLAW_SSH_HOST", "")
    repo_dir: str = os.getenv("OPENCLAW_REPO_DIR", "")
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_repo: str = os.getenv("GITHUB_REPO", "leeheggan-droid/openclaw-control")
    vibe_workdir: str = os.getenv("OPENCLAW_VIBE_WORKDIR", "")

settings = Settings()