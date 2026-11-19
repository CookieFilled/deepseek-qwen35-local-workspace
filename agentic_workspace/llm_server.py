"""Cross-platform local llama.cpp inference server manager (native llama-server or llama-cpp-python)."""

from __future__ import annotations

import atexit
import glob
import os
import platform
import shutil
import site
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .config import WorkspaceConfig
from .models import ModelCacheManager


class LlamaServerManager:
    """Manages lifecycle, GPU detection, fallback, and watchdog for local llama.cpp inference."""

    def __init__(self, config: WorkspaceConfig, model_cache: ModelCacheManager) -> None:
        self.config = config
        self.model_cache = model_cache

        self.llm_lock = threading.RLock()
        self._restart_lock = threading.Lock()
        self.proc: Optional[subprocess.Popen] = None
        self.engine: str = "unresolved"
        self.native_bin: Optional[str] = None
        self.model_path: Optional[str] = None
        self.mmproj_path: Optional[str] = None
        self.vision_active: bool = False
        self.crash_count: int = 0
        self._help_out: str = ""
        self._watchdog_started: bool = False
        self._stopping: bool = False
        self._log_file = None

        atexit.register(self.stop)

    @staticmethod
    def _collect_cuda_ld_path() -> str:
        if platform.system() != "Linux":
            return os.environ.get("LD_LIBRARY_PATH", "")
        dirs = []
        for p in glob.glob("/usr/local/cuda*/lib64") + ["/usr/local/nvidia/lib", "/usr/local/nvidia/lib64"]:
            if os.path.isdir(p):
                dirs.append(p)
        try:
            site_dirs = list(site.getsitepackages()) + [site.getusersitepackages()]
        except Exception:
            site_dirs = []
        for sp in site_dirs:
            for p in glob.glob(os.path.join(sp, "nvidia", "*", "lib")):
                if os.path.isdir(p):
                    dirs.append(p)
        existing = [d for d in os.environ.get("LD_LIBRARY_PATH", "").split(":") if d]
        merged = []
        for d in dirs + existing:
            if d not in merged:
                merged.append(d)
        return ":".join(merged)

    @staticmethod
    def detect_gpu_layers(requested: int) -> int:
        """Auto-detect whether CUDA or Apple Metal is available; fall back to CPU (0) if none."""
        if requested == 0:
            return 0
        sys_name = platform.system()
        if sys_name == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            return requested
        if shutil.which("nvidia-smi"):
            try:
                r = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                    capture_output=True, text=True, timeout=5,
                )
                if r.returncode == 0 and r.stdout.strip():
                    return requested
            except Exception:
                pass
        return 0

    def resolve_engine(self) -> str:
        """Find native llama-server on PATH / bin_dir or fall back to llama_cpp.server."""
        exe_names = ["llama-server.exe", "llama-server"] if platform.system() == "Windows" else ["llama-server"]
        candidates = []
        if self.config.llama_server_bin:
            candidates.append(self.config.llama_server_bin)
        for name in exe_names:
            local_cand = self.config.bin_dir / name
            if local_cand.is_file():
                candidates.append(str(local_cand))
            found = shutil.which(name)
            if found:
                candidates.append(found)

        env = dict(os.environ)
        ld = self._collect_cuda_ld_path()
        if ld:
            env["LD_LIBRARY_PATH"] = ld

        for cand in candidates:
            try:
                r = subprocess.run([cand, "--help"], capture_output=True, text=True, env=env, timeout=10)
                if r.returncode == 0:
                    self.native_bin = cand
                    self.engine = "native"
                    self._help_out = r.stdout
                    if ld:
                        os.environ["LD_LIBRARY_PATH"] = ld
                    return self.engine
            except Exception:
                continue

        # Fallback to llama-cpp-python server
        try:
            r = subprocess.run(
                [sys.executable, "-m", "llama_cpp.server", "--help"],
                capture_output=True, text=True, timeout=15,
            )
            if r.returncode == 0:
                self.engine = "python"
                self._help_out = r.stdout
                return self.engine
        except Exception:
            pass

        raise RuntimeError(
            "No local inference backend found. Install llama.cpp ('llama-server' on PATH or in ./bin/) "
            "or install llama-cpp-python[server], or run with --provider gemini."
        )

    def _tcp_port_open(self, timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((self.config.llm_host, self.config.llm_port), timeout=timeout):
                return True
        except Exception:
            return False

    def is_up(self, timeout: float = 2.0) -> bool:
        if self.proc is not None and self.proc.poll() is not None:
            return False
        if self.proc is None and not self._tcp_port_open(timeout=0.5):
            return False
        if self.engine == "native":
            for ep in ("/health", "/v1/models"):
                try:
                    with urllib.request.urlopen(f"{self.config.llm_base_url}{ep}", timeout=timeout) as r:
                        if r.status == 200:
                            return True
                except Exception:
                    continue
            return False
        if self.llm_lock.acquire(blocking=False):
            try:
                with urllib.request.urlopen(f"{self.config.llm_base_url}/v1/models", timeout=timeout) as r:
                    return r.status == 200
            except Exception:
                return False
            finally:
                self.llm_lock.release()
        return self._tcp_port_open(timeout=1.0)

    def log_tail(self, n_chars: int = 3000) -> str:
        try:
            if self.config.server_log_path.is_file():
                return self.config.server_log_path.read_text(encoding="utf-8", errors="replace")[-n_chars:]
        except Exception:
            pass
        return "(no server log yet)"

    def _build_cmd(self, use_vision: bool, safe_fallback: bool = False) -> list[str]:
        eff_ctx = int(self.config.context_size)
        gpu_layers = self.detect_gpu_layers(self.config.gpu_layers)
        n_threads = max(2, min(os.cpu_count() or 4, 8))

        if self.engine == "native" and self.native_bin:
            fa_mode = "off" if (safe_fallback or gpu_layers == 0) else "on"
            cmd = [
                self.native_bin,
                "--model", str(self.model_path),
                "--alias", self.config.model_alias,
                "--host", self.config.llm_host,
                "--port", str(self.config.llm_port),
                "--n-gpu-layers", str(gpu_layers),
                "--ctx-size", str(eff_ctx),
                "--batch-size", "256" if not safe_fallback else "128",
                "--ubatch-size", "64" if not safe_fallback else "32",
                "--threads", str(n_threads),
                "--parallel", "1",
                "--flash-attn", fa_mode,
                "--no-webui",
                "--cont-batching",
            ]
            if "--cache-type-k" in self._help_out:
                cmd.extend(["--cache-type-k", "q8_0"])
            elif "--ctk" in self._help_out:
                cmd.extend(["--ctk", "q8_0"])
            if not safe_fallback:
                if "--cache-type-v" in self._help_out:
                    cmd.extend(["--cache-type-v", "q8_0"])
                elif "--ctv" in self._help_out:
                    cmd.extend(["--ctv", "q8_0"])
            if use_vision and self.mmproj_path:
                cmd.extend(["--mmproj", str(self.mmproj_path)])
            return cmd

        cmd = [
            sys.executable, "-m", "llama_cpp.server",
            "--model", str(self.model_path),
            "--model_alias", self.config.model_alias,
            "--host", self.config.llm_host,
            "--port", str(self.config.llm_port),
            "--n_gpu_layers", str(gpu_layers),
            "--n_ctx", str(eff_ctx),
            "--n_batch", "128" if safe_fallback else "256",
            "--n_ubatch", "64" if safe_fallback else "128",
            "--n_threads", str(n_threads),
        ]
        if "--interrupt_requests" in self._help_out:
            cmd.extend(["--interrupt_requests", "False"])
        if "--cache_type_k" in self._help_out:
            cmd.extend(["--cache_type_k", "q8_0"])
        elif "--type_k" in self._help_out:
            cmd.extend(["--type_k", "8"])

        if use_vision and self.mmproj_path:
            chosen_fmt = "chatml"
            try:
                from llama_cpp.llama_chat_format import CHAT_HANDLERS  # type: ignore

                for candidate in ("qwen2.5-vl", "qwen3-vl", "qwen3.5", "llava-1-5"):
                    if candidate in CHAT_HANDLERS:
                        chosen_fmt = candidate
                        break
            except Exception:
                pass
            cmd.extend(["--clip_model_path", str(self.mmproj_path), "--chat_format", chosen_fmt])
        else:
            cmd.extend(["--chat_format", "chatml"])
        return cmd

    def stop(self) -> None:
        self._stopping = True
        if self.proc is not None:
            try:
                if self.proc.poll() is None:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                        self.proc.wait(timeout=3)
            except Exception:
                pass
            self.proc = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def start(self, force_restart: bool = False, safe_fallback: bool = False, download_if_missing: bool = True) -> bool:
        with self._restart_lock:
            self._stopping = False
            if not force_restart and self.is_up(timeout=2.0):
                return True

            resolved = self.model_cache.ensure_models(download_if_missing=download_if_missing)
            if not resolved.model_path:
                raise FileNotFoundError(
                    f"No local GGUF model found in {self.config.models_dir}. "
                    "Download a model or switch provider to Gemini."
                )
            self.model_path = resolved.model_path
            self.mmproj_path = resolved.mmproj_path

            if self.engine == "unresolved":
                self.resolve_engine()

            self.stop()
            self._stopping = False

            use_vis = (self.mmproj_path is not None) and (not safe_fallback)
            cmd = self._build_cmd(use_vision=use_vis, safe_fallback=safe_fallback)
            server_env = {
                **os.environ,
                "GGML_CUDA_FORCE_MMQ": "1",
                "GGML_CUDA_ENABLE_UNIFIED_MEMORY": "1",
                "PYTHONUNBUFFERED": "1",
            }
            ld = self._collect_cuda_ld_path()
            if ld:
                server_env["LD_LIBRARY_PATH"] = ld

            self.config.logs_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self.config.server_log_path, "a", encoding="utf-8")
            self._log_file.write(
                f"\n=== [Launch {time.strftime('%H:%M:%S')}] engine={self.engine} cmd={' '.join(cmd)} ===\n"
            )
            self._log_file.flush()

            popen_kwargs: dict = {
                "stdout": self._log_file,
                "stderr": subprocess.STDOUT,
                "env": server_env,
            }
            if platform.system() != "Windows":
                popen_kwargs["start_new_session"] = True
            self.proc = subprocess.Popen(cmd, **popen_kwargs)

            for _ in range(150):
                if self.proc.poll() is not None:
                    logs = self.log_tail(3000)
                    if use_vis or not safe_fallback:
                        print(f"[Server] Startup recovery triggered (preserving {self.config.context_size} ctx). Retrying...")
                        use_vis = False
                        safe_fallback = True
                        cmd = self._build_cmd(use_vision=False, safe_fallback=True)
                        self._log_file.write(
                            f"\n=== [Fallback Launch {time.strftime('%H:%M:%S')}] cmd={' '.join(cmd)} ===\n"
                        )
                        self._log_file.flush()
                        self.proc = subprocess.Popen(cmd, **popen_kwargs)
                        continue
                    raise RuntimeError(f"llama.cpp server exited unexpectedly:\n{logs}")

                if self._tcp_port_open(timeout=1.0):
                    try:
                        with urllib.request.urlopen(f"{self.config.llm_base_url}/v1/models", timeout=2.5) as r:
                            if r.status == 200:
                                self.vision_active = use_vis
                                self._ensure_watchdog()
                                return True
                    except Exception:
                        pass
                time.sleep(1.2)

            raise TimeoutError(f"Server timed out starting. Logs:\n{self.log_tail()}")

    def _ensure_watchdog(self) -> None:
        if self._watchdog_started:
            return
        self._watchdog_started = True

        def _loop() -> None:
            while True:
                time.sleep(4.0)
                if self._stopping or self.config.provider == "gemini":
                    continue
                if self.proc is not None and self.proc.poll() is not None:
                    self.crash_count += 1
                    print(
                        f"\n[Watchdog] llama.cpp exited (code {self.proc.returncode}, crash #{self.crash_count}). Restarting..."
                    )
                    try:
                        self.start(force_restart=True, safe_fallback=(self.crash_count >= 3), download_if_missing=False)
                        print("[Watchdog] llama.cpp server restored.")
                    except Exception as e:
                        print(f"[Watchdog] Recovery error: {e}")

        threading.Thread(target=_loop, daemon=True).start()
