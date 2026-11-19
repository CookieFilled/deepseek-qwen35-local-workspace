"""Cross-platform configuration and persistent path management."""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _load_dotenv(env_path: Path) -> None:
    """Load key=value pairs from a .env file without requiring python-dotenv."""
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(env_path, override=False)
        return
    except ImportError:
        pass

    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _default_base_dir() -> Path:
    env_home = os.environ.get("AGENT_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


@dataclass
class WorkspaceConfig:
    base_dir: Path = field(default_factory=_default_base_dir)
    workspace_dir: Path = field(init=False)
    models_dir: Path = field(init=False)
    bin_dir: Path = field(init=False)
    logs_dir: Path = field(init=False)
    skills_dir: Path = field(init=False)
    artifacts_dir: Path = field(init=False)
    images_dir: Path = field(init=False)
    screenshots_dir: Path = field(init=False)
    uploads_dir: Path = field(init=False)
    latest_screen_path: Path = field(init=False)
    server_log_path: Path = field(init=False)
    cloudflare_log_path: Path = field(init=False)
    runtime_config_path: Path = field(init=False)

    # Provider & API settings
    provider: str = "local"  # "local" or "gemini"
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    skip_local_model: bool = False

    # Local GGUF model settings
    model_repo: str = "mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF"
    quantization: str = "Q4_K_M"
    enable_vision_mmproj: bool = True
    mmproj_repo: str = "mradermacher/DeepSeek-V4-Pro-Qwen3.5-9B-UNCENSORED-heretic-GGUF"
    local_model_path: Optional[str] = None
    local_mmproj_path: Optional[str] = None
    llama_server_bin: Optional[str] = None
    model_alias: str = "qwen35-agent"

    # Inference & context settings
    context_size: int = 32768
    temperature: float = 0.6
    max_new_tokens_per_call: int = 8192
    soft_context_budget: int = 12000
    gpu_layers: int = -1

    # Network & UI settings
    server_host: str = "127.0.0.1"
    server_port: int = 7860
    llm_host: str = "127.0.0.1"
    llm_port: int = 8000
    enable_cloudflare_tunnel: bool = False

    # Desktop & Browser settings
    enable_virtual_desktop: bool = False
    browser_headless: bool = True

    def __post_init__(self) -> None:
        _load_dotenv(self.base_dir / ".env")

        self.workspace_dir = Path(
            os.environ.get("WORKSPACE_DIR") or (self.base_dir / "workspace")
        ).expanduser().resolve()
        self.models_dir = Path(
            os.environ.get("MODELS_DIR") or (self.base_dir / "models")
        ).expanduser().resolve()
        self.bin_dir = Path(
            os.environ.get("BIN_DIR") or (self.base_dir / "bin")
        ).expanduser().resolve()
        self.logs_dir = Path(
            os.environ.get("LOGS_DIR") or (self.base_dir / "logs")
        ).expanduser().resolve()

        self.skills_dir = self.workspace_dir / "skills"
        self.artifacts_dir = self.workspace_dir / "artifacts"
        self.images_dir = self.artifacts_dir / "images"
        self.screenshots_dir = self.workspace_dir / "screenshots"
        self.uploads_dir = self.workspace_dir / "uploads"
        self.latest_screen_path = self.screenshots_dir / "latest_screen.png"
        self.server_log_path = self.logs_dir / "llama_server.log"
        self.cloudflare_log_path = self.logs_dir / "cloudflared.log"
        self.runtime_config_path = self.base_dir / "studio_runtime.json"

        self.provider = (os.environ.get("LLM_PROVIDER") or self.provider).strip().lower()
        if self.provider not in ("local", "gemini"):
            self.provider = "local"
        self.gemini_api_key = (os.environ.get("GEMINI_API_KEY") or self.gemini_api_key).strip()
        self.gemini_model = (os.environ.get("GEMINI_MODEL") or self.gemini_model).strip()
        self.skip_local_model = _env_bool("SKIP_LOCAL_MODEL", self.provider == "gemini")

        self.model_repo = (os.environ.get("MODEL_REPO") or self.model_repo).strip()
        self.quantization = (os.environ.get("QUANTIZATION") or self.quantization).strip()
        self.enable_vision_mmproj = _env_bool("ENABLE_VISION_MMPROJ", self.enable_vision_mmproj)
        self.mmproj_repo = (os.environ.get("MMPROJ_REPO") or self.model_repo).strip()
        self.local_model_path = (os.environ.get("LOCAL_MODEL_PATH") or "").strip() or None
        self.local_mmproj_path = (os.environ.get("LOCAL_MMPROJ_PATH") or "").strip() or None
        self.llama_server_bin = (os.environ.get("LLAMA_SERVER_BIN") or "").strip() or None

        self.context_size = _env_int("CONTEXT_SIZE", self.context_size)
        self.temperature = _env_float("TEMPERATURE", self.temperature)
        self.max_new_tokens_per_call = _env_int("MAX_NEW_TOKENS_PER_CALL", self.max_new_tokens_per_call)
        self.soft_context_budget = _env_int("SOFT_CONTEXT_BUDGET", self.soft_context_budget)
        self.gpu_layers = _env_int("GPU_LAYERS", self.gpu_layers)

        self.server_host = (os.environ.get("SERVER_HOST") or self.server_host).strip()
        self.server_port = _env_int("SERVER_PORT", self.server_port)
        self.llm_host = (os.environ.get("LLM_HOST") or self.llm_host).strip()
        self.llm_port = _env_int("LLM_PORT", self.llm_port)
        self.enable_cloudflare_tunnel = _env_bool("ENABLE_CLOUDFLARE_TUNNEL", self.enable_cloudflare_tunnel)

        self.enable_virtual_desktop = _env_bool("ENABLE_VIRTUAL_DESKTOP", self.enable_virtual_desktop)
        self.browser_headless = _env_bool("BROWSER_HEADLESS", self.browser_headless)

        # Load persisted UI overrides (e.g. saved Gemini key/model) if present
        self._load_runtime_overrides()
        self.ensure_dirs()

    def ensure_dirs(self) -> None:
        for d in (
            self.workspace_dir,
            self.models_dir,
            self.bin_dir,
            self.logs_dir,
            self.skills_dir,
            self.artifacts_dir,
            self.images_dir,
            self.screenshots_dir,
            self.uploads_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def _load_runtime_overrides(self) -> None:
        if not self.runtime_config_path.is_file():
            return
        try:
            data = json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
            if not self.gemini_api_key and data.get("gemini_api_key"):
                self.gemini_api_key = str(data["gemini_api_key"]).strip()
            if data.get("gemini_model") and not os.environ.get("GEMINI_MODEL"):
                self.gemini_model = str(data["gemini_model"]).strip()
        except Exception:
            pass

    def save_runtime_overrides(self, *, provider: Optional[str] = None, gemini_api_key: Optional[str] = None, gemini_model: Optional[str] = None) -> None:
        data = {}
        if self.runtime_config_path.is_file():
            try:
                data = json.loads(self.runtime_config_path.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        if provider is not None:
            data["provider"] = provider
        if gemini_api_key is not None:
            data["gemini_api_key"] = gemini_api_key
        if gemini_model is not None:
            data["gemini_model"] = gemini_model
        try:
            self.runtime_config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    @property
    def llm_base_url(self) -> str:
        return f"http://{self.llm_host}:{self.llm_port}"

    @property
    def os_name(self) -> str:
        return platform.system()
