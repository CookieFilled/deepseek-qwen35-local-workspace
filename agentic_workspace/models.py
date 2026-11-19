"""Persistent local GGUF & mmproj model cache with optional on-demand downloading."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import WorkspaceConfig

MIN_MODEL_BYTES = 50_000_000   # 50 MB sanity floor to ignore partial/corrupt files
MIN_MMPROJ_BYTES = 10_000_000  # 10 MB sanity floor for vision projector


@dataclass
class ResolvedModels:
    model_path: Optional[str] = None
    mmproj_path: Optional[str] = None
    from_cache: bool = False


class ModelCacheManager:
    """Manages persistent local GGUF files so models are never redownloaded across restarts."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        self.manifest_path = config.models_dir / "model_manifest.json"
        self._dl_lock = threading.Lock()
        self.download_state: dict = {
            "in_progress": False,
            "status": "idle",
            "error": "",
        }

    def _read_manifest(self) -> dict:
        if self.manifest_path.is_file():
            try:
                return json.loads(self.manifest_path.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_manifest(self, model_path: str, mmproj_path: Optional[str]) -> None:
        payload = {
            "model_repo": self.config.model_repo,
            "quantization": self.config.quantization,
            "mmproj_repo": self.config.mmproj_repo,
            "model_path": str(Path(model_path).resolve()),
            "mmproj_path": str(Path(mmproj_path).resolve()) if mmproj_path else None,
        }
        try:
            self.manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception:
            pass

    @staticmethod
    def _is_valid_file(path: Optional[str], min_bytes: int) -> bool:
        if not path:
            return False
        p = Path(path)
        return p.is_file() and p.stat().st_size >= min_bytes

    def find_cached(self) -> ResolvedModels:
        """Check local disk ONLY (zero network calls) for already-downloaded GGUF & mmproj."""
        # 1. Explicit paths configured via env / CLI
        explicit_model = self.config.local_model_path
        explicit_mmproj = self.config.local_mmproj_path
        if self._is_valid_file(explicit_model, MIN_MODEL_BYTES):
            mm = explicit_mmproj if self._is_valid_file(explicit_mmproj, MIN_MMPROJ_BYTES) else self._scan_mmproj()
            return ResolvedModels(model_path=str(Path(explicit_model).resolve()), mmproj_path=mm, from_cache=True)

        # 2. Manifest hit matching current repo & quantization
        manifest = self._read_manifest()
        if (
            manifest.get("model_repo") == self.config.model_repo
            and str(manifest.get("quantization", "")).lower() == self.config.quantization.lower()
            and self._is_valid_file(manifest.get("model_path"), MIN_MODEL_BYTES)
        ):
            mm = manifest.get("mmproj_path")
            if not self.config.enable_vision_mmproj:
                mm = None
            elif not self._is_valid_file(mm, MIN_MMPROJ_BYTES):
                mm = self._scan_mmproj()
            return ResolvedModels(model_path=manifest["model_path"], mmproj_path=mm, from_cache=True)

        # 3. Direct scan of models_dir for matching quantization GGUF
        quant_lower = self.config.quantization.lower()
        candidates = []
        for p in self.config.models_dir.rglob("*.gguf"):
            if not p.is_file() or p.stat().st_size < MIN_MODEL_BYTES:
                continue
            name_low = p.name.lower()
            if "mmproj" in name_low:
                continue
            if quant_lower in name_low:
                candidates.append(p)

        if candidates:
            chosen = sorted(candidates, key=lambda x: len(x.name))[0]
            mm = self._scan_mmproj() if self.config.enable_vision_mmproj else None
            self._write_manifest(str(chosen), mm)
            return ResolvedModels(model_path=str(chosen.resolve()), mmproj_path=mm, from_cache=True)

        return ResolvedModels(model_path=None, mmproj_path=self._scan_mmproj() if self.config.enable_vision_mmproj else None, from_cache=False)

    def _scan_mmproj(self) -> Optional[str]:
        if self._is_valid_file(self.config.local_mmproj_path, MIN_MMPROJ_BYTES):
            return str(Path(self.config.local_mmproj_path).resolve())
        mm_candidates = [
            p for p in self.config.models_dir.rglob("*.gguf")
            if p.is_file() and "mmproj" in p.name.lower() and p.stat().st_size >= MIN_MMPROJ_BYTES
        ]
        if mm_candidates:
            return str(sorted(mm_candidates, key=lambda x: x.name)[0].resolve())
        return None

    def ensure_models(self, download_if_missing: bool = True) -> ResolvedModels:
        """Return cached GGUF paths immediately, or download once from Hugging Face if allowed."""
        cached = self.find_cached()
        if cached.model_path and (not self.config.enable_vision_mmproj or cached.mmproj_path):
            print(f"[ModelCache] Using cached GGUF (no download needed): {cached.model_path}")
            if cached.mmproj_path:
                print(f"[ModelCache] Using cached Vision mmproj: {cached.mmproj_path}")
            return cached

        if not download_if_missing:
            return cached

        with self._dl_lock:
            # Re-check inside lock
            cached = self.find_cached()
            if cached.model_path and (not self.config.enable_vision_mmproj or cached.mmproj_path):
                return cached

            self.download_state = {"in_progress": True, "status": "inspecting Hugging Face repo...", "error": ""}
            try:
                from huggingface_hub import HfApi, hf_hub_download

                api = HfApi()
                model_path = cached.model_path
                mmproj_path = cached.mmproj_path
                repo_files = None

                if not model_path:
                    print(f"[HF] Inspecting repository: {self.config.model_repo} ...")
                    repo_files = api.list_repo_files(repo_id=self.config.model_repo)
                    gguf_files = [
                        f for f in repo_files
                        if f.lower().endswith(".gguf") and "mmproj" not in f.lower()
                    ]
                    matched = [f for f in gguf_files if self.config.quantization.lower() in f.lower()]
                    if not matched:
                        raise FileNotFoundError(
                            f"Could not find quantization '{self.config.quantization}' in {self.config.model_repo}. "
                            f"Available: {gguf_files}"
                        )
                    target_gguf = sorted(matched, key=len)[0]
                    self.download_state["status"] = f"downloading {target_gguf}..."
                    print(f"[HF] Downloading GGUF once to persistent cache: {target_gguf}")
                    model_path = hf_hub_download(
                        repo_id=self.config.model_repo,
                        filename=target_gguf,
                        local_dir=str(self.config.models_dir),
                    )
                    print(f"[OK] Model cached at: {model_path}")

                if self.config.enable_vision_mmproj and not mmproj_path:
                    try:
                        mm_source_files = (
                            repo_files
                            if (repo_files is not None and self.config.mmproj_repo == self.config.model_repo)
                            else api.list_repo_files(repo_id=self.config.mmproj_repo)
                        )
                        mm_files = [
                            f for f in mm_source_files
                            if "mmproj" in f.lower() and f.lower().endswith(".gguf")
                        ]
                        if mm_files:
                            chosen_mm = sorted(mm_files)[0]
                            self.download_state["status"] = f"downloading {chosen_mm}..."
                            print(f"[HF] Downloading Vision Projector once: {chosen_mm}")
                            mmproj_path = hf_hub_download(
                                repo_id=self.config.mmproj_repo,
                                filename=chosen_mm,
                                local_dir=str(self.config.models_dir),
                            )
                            print(f"[OK] Vision mmproj cached at: {mmproj_path}")
                    except Exception as e:
                        print(f"[Warning] Could not fetch mmproj (continuing in text-only mode): {e}")
                        mmproj_path = None

                if model_path:
                    self._write_manifest(model_path, mmproj_path)

                self.download_state = {"in_progress": False, "status": "cached", "error": ""}
                return ResolvedModels(model_path=model_path, mmproj_path=mmproj_path, from_cache=False)
            except Exception as e:
                self.download_state = {"in_progress": False, "status": "error", "error": str(e)}
                raise

    def status_summary(self) -> dict:
        cached = self.find_cached()
        return {
            "models_dir": str(self.config.models_dir),
            "model_cached": cached.model_path is not None,
            "model_path": cached.model_path,
            "model_name": os.path.basename(cached.model_path) if cached.model_path else None,
            "mmproj_cached": cached.mmproj_path is not None,
            "mmproj_path": cached.mmproj_path,
            "mmproj_name": os.path.basename(cached.mmproj_path) if cached.mmproj_path else None,
            "model_repo": self.config.model_repo,
            "quantization": self.config.quantization,
            "download_in_progress": self.download_state.get("in_progress", False),
            "download_status": self.download_state.get("status", "idle"),
            "download_error": self.download_state.get("error", ""),
        }
