#!/usr/bin/env python3
"""Unified CLI entrypoint for the Local Autonomous Agentic Workspace."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

import uvicorn

from agentic_workspace.config import WorkspaceConfig
from agentic_workspace.models import ModelCacheManager
from agentic_workspace.server import StudioRuntime, create_app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="DeepSeek-V4-Pro / Qwen3.5 Local Autonomous Agentic Workspace",
    )
    p.add_argument(
        "--provider",
        choices=["local", "gemini"],
        default=None,
        help="Active LLM provider. Use 'gemini' to skip local model download and GPU setup.",
    )
    p.add_argument("--gemini-key", default=None, help="Gemini API key (overrides GEMINI_API_KEY env var).")
    p.add_argument("--gemini-model", default=None, help="Gemini model ID (e.g. gemini-2.5-flash, gemini-2.5-pro).")
    p.add_argument(
        "--skip-local-model",
        action="store_true",
        help="Start the web UI without downloading or booting the local GGUF server.",
    )
    p.add_argument(
        "--download-only",
        action="store_true",
        help="Download and cache the local GGUF + mmproj model once, then exit.",
    )
    p.add_argument("--model-path", default=None, help="Path to an existing local .gguf model file.")
    p.add_argument("--mmproj-path", default=None, help="Path to an existing local mmproj .gguf file.")
    p.add_argument("--host", default=None, help="Host interface for the Studio UI (default: 127.0.0.1).")
    p.add_argument("--port", type=int, default=None, help="Port for the Studio UI (default: 7860).")
    p.add_argument("--tunnel", action="store_true", help="Expose Studio UI via Cloudflare Quick Tunnel (requires cloudflared).")
    p.add_argument("--virtual-desktop", action="store_true", help="Enable Linux Xvfb + Fluxbox + x11vnc virtual desktop.")
    p.add_argument("--open-browser", action="store_true", help="Automatically open the Studio UI in your default browser.")
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Apply CLI overrides before building WorkspaceConfig
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    if args.gemini_key:
        os.environ["GEMINI_API_KEY"] = args.gemini_key
    if args.gemini_model:
        os.environ["GEMINI_MODEL"] = args.gemini_model
    if args.skip_local_model:
        os.environ["SKIP_LOCAL_MODEL"] = "true"
    if args.model_path:
        os.environ["LOCAL_MODEL_PATH"] = args.model_path
    if args.mmproj_path:
        os.environ["LOCAL_MMPROJ_PATH"] = args.mmproj_path
    if args.host:
        os.environ["SERVER_HOST"] = args.host
    if args.port:
        os.environ["SERVER_PORT"] = str(args.port)
    if args.tunnel:
        os.environ["ENABLE_CLOUDFLARE_TUNNEL"] = "true"
    if args.virtual_desktop:
        os.environ["ENABLE_VIRTUAL_DESKTOP"] = "true"

    config = WorkspaceConfig()

    if args.download_only:
        cache = ModelCacheManager(config)
        res = cache.ensure_models(download_if_missing=True)
        print(f"[OK] Model cached at: {res.model_path}")
        if res.mmproj_path:
            print(f"[OK] Vision mmproj cached at: {res.mmproj_path}")
        return 0

    runtime = StudioRuntime(config)

    # Boot local llama.cpp ONLY when provider == "local" and not explicitly skipped
    if config.provider == "local" and not config.skip_local_model:
        print(f"[Boot] Provider=LOCAL | Persistent model cache: {config.models_dir}")
        try:
            runtime.llm_server.start(force_restart=False, download_if_missing=True)
            print(
                f"[OK] Local LLM Server Online | Engine: {runtime.llm_server.engine} | "
                f"Context: {config.context_size} | Vision: {runtime.llm_server.vision_active}"
            )
        except Exception as e:
            print(f"[Warning] Local LLM server did not start: {e}")
            print("[Info] Studio UI will still launch — switch to GEMINI or start local inference from the System tab.")
    else:
        print(
            f"[Boot] Provider={config.provider.upper()} | Skipping local model download & llama-server startup. "
            "(You can still download/start the local model anytime from the System tab.)"
        )

    if config.enable_cloudflare_tunnel:
        url = runtime.start_cloudflare_tunnel()
        if url:
            print(f"[Tunnel] Public Cloudflare URL: {url}")

    local_url = f"http://{config.server_host}:{config.server_port}"
    print(f"\n[Studio] Local Web UI ready at: {local_url}\n")

    if args.open_browser:
        def _open():
            time.sleep(1.5)
            webbrowser.open(local_url)

        threading.Thread(target=_open, daemon=True).start()

    app = create_app(runtime)
    try:
        uvicorn.run(app, host=config.server_host, port=config.server_port, log_level="warning")
    except KeyboardInterrupt:
        print("\n[Shutdown] Stopping services...")
    finally:
        runtime.llm_server.stop()
        if runtime.cf_proc is not None and runtime.cf_proc.poll() is None:
            runtime.cf_proc.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
