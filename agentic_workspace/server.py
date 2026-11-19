"""FastAPI Studio web backend, SSE streaming, noVNC proxy, optional Cloudflare tunnel, and model download API."""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .agent import AgentEngine, est_tokens, render_chatml
from .browser import HeadlessBrowserWorker
from .config import WorkspaceConfig
from .desktop import DesktopOperator
from .llm_server import LlamaServerManager
from .models import ModelCacheManager
from .skills import SkillManager

STATIC_DIR = Path(__file__).resolve().parent / "static"


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class ChatReq(BaseModel):
    message: str
    reasoning_mode: str = "brief"
    max_steps: int = 24
    auto_vision: bool = True


class GithubSkillReq(BaseModel):
    repo_url: str
    subpath: str = ""


class ProviderReq(BaseModel):
    provider: str = "local"
    gemini_api_key: Optional[str] = None
    gemini_model: Optional[str] = None


class StudioRuntime:
    """Holds all runtime services for the local Studio web application."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        self.model_cache = ModelCacheManager(config)
        self.llm_server = LlamaServerManager(config, self.model_cache)
        self.skill_manager = SkillManager(config.skills_dir, config.workspace_dir)
        self.browser_worker = HeadlessBrowserWorker(config)
        self.desktop_operator = DesktopOperator(config)
        self.agent = AgentEngine(
            config=config,
            llm_server=self.llm_server,
            skill_manager=self.skill_manager,
            browser_worker=self.browser_worker,
            desktop_operator=self.desktop_operator,
        )
        self.agent_lock = threading.Lock()
        self.ui_messages: list[dict] = []
        self.llm_messages: list[dict] = []
        self.tunnel_url: str = ""
        self.cf_proc: Optional[subprocess.Popen] = None

    def start_cloudflare_tunnel(self) -> str:
        if not self.config.enable_cloudflare_tunnel:
            return ""
        cf_bin = shutil.which("cloudflared")
        if not cf_bin:
            local_cf = self.config.bin_dir / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
            if local_cf.is_file():
                cf_bin = str(local_cf)
        if not cf_bin:
            print("[Tunnel] 'cloudflared' not found on PATH or ./bin/. Skipping public tunnel.")
            return ""

        log_f = open(self.config.cloudflare_log_path, "w", encoding="utf-8")
        popen_kwargs: dict = {"stdout": log_f, "stderr": subprocess.STDOUT}
        if platform.system() != "Windows":
            popen_kwargs["start_new_session"] = True
        self.cf_proc = subprocess.Popen(
            [cf_bin, "tunnel", "--url", f"http://{self.config.server_host}:{self.config.server_port}", "--no-autoupdate"],
            **popen_kwargs,
        )
        deadline = time.time() + 25.0
        while time.time() < deadline:
            if self.cf_proc.poll() is not None:
                break
            try:
                txt = self.config.cloudflare_log_path.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com", txt)
                if m:
                    self.tunnel_url = m.group(0)
                    return self.tunnel_url
            except Exception:
                pass
            time.sleep(0.5)
        return ""

    def list_artifacts_meta(self) -> list[dict]:
        root = self.config.artifacts_dir
        root.mkdir(parents=True, exist_ok=True)
        files = [p for p in root.rglob("*") if p.is_file() and not p.name.startswith(".")]
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:200]:
            st = p.stat()
            rel = p.relative_to(root).as_posix()
            out.append({
                "name": p.name,
                "rel_path": rel,
                "path": str(p),
                "size": st.st_size,
                "size_str": _fmt_size(st.st_size),
                "mtime": int(st.st_mtime),
                "ext": p.suffix.lower(),
                "url": f"/api/files/artifact/{rel}?t={int(st.st_mtime)}",
            })
        return out

    def list_images_meta(self) -> list[dict]:
        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        imgs = [
            p for p in self.config.images_dir.glob("*")
            if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".gif")
        ]
        imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return [
            {
                "name": p.name,
                "path": str(p),
                "url": f"/api/files/artifact/images/{p.name}?t={int(p.stat().st_mtime)}",
                "size_str": _fmt_size(p.stat().st_size),
                "mtime": int(p.stat().st_mtime),
            }
            for p in imgs[:80]
        ]

    def get_latest_video_meta(self) -> Optional[dict]:
        for item in self.list_artifacts_meta():
            if item["ext"] in (".mp4", ".webm", ".mov"):
                return item
        return None

    def get_latest_screen_meta(self) -> Optional[dict]:
        if self.config.latest_screen_path.is_file():
            st = self.config.latest_screen_path.stat()
            return {
                "url": f"/api/files/screen?t={int(st.st_mtime)}",
                "mtime": int(st.st_mtime),
                "path": str(self.config.latest_screen_path),
            }
        return None

    def get_skills_meta(self) -> list[dict]:
        self.skill_manager.refresh()
        return [
            {
                "name": info["name"],
                "description": info["description"],
                "path": info["path"],
                "folder": info["folder"],
                "resources": info["resources"],
            }
            for info in self.skill_manager.registry.values()
        ]

    def get_system_meta(self) -> dict:
        up = self.llm_server.is_up()
        gpu_name, vram_used, vram_total, gpu_util = "CPU / Integrated", 0, 0, 0
        if platform.system() == "Darwin" and platform.machine().lower() in ("arm64", "aarch64"):
            gpu_name = "Apple Silicon (Metal)"
        elif shutil.which("nvidia-smi"):
            try:
                g = subprocess.run(
                    ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=6,
                ).stdout.strip().split(",")
                if len(g) >= 4:
                    gpu_name = g[0].strip()
                    vram_used = int(float(g[1].strip()))
                    vram_total = int(float(g[2].strip()))
                    gpu_util = int(float(g[3].strip()))
            except Exception:
                pass

        return {
            "server_up": up,
            "engine": self.llm_server.engine,
            "vision_active": bool(self.llm_server.vision_active or (self.config.provider == "gemini" and self.config.gemini_api_key)),
            "context_size": int(self.config.context_size),
            "temperature": float(self.config.temperature),
            "crash_count": int(self.llm_server.crash_count),
            "gpu_name": gpu_name,
            "vram_used": vram_used,
            "vram_total": vram_total,
            "gpu_util": gpu_util,
            "server_log": self.llm_server.log_tail(2800),
            "tunnel_url": self.tunnel_url,
            "provider": self.config.provider,
            "gemini_model": self.config.gemini_model,
            "has_gemini_key": bool((self.config.gemini_api_key or "").strip()),
            "workspace_dir": self.config.workspace_dir.as_posix(),
            "artifacts_dir": self.config.artifacts_dir.as_posix(),
            "models": self.model_cache.status_summary(),
            "os_name": platform.system(),
        }

    def context_used_tokens(self) -> int:
        try:
            return est_tokens(render_chatml(self.llm_messages))
        except Exception:
            return 0


def create_app(runtime: StudioRuntime) -> FastAPI:
    app = FastAPI(title="DeepSeek-V4-Pro · Qwen3.5 Local Studio")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    def index():
        html_path = STATIC_DIR / "index.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    @app.get("/api/health")
    def api_health():
        return {"ok": True, "ts": time.time()}

    @app.get("/api/state")
    def api_state():
        return {
            "messages": runtime.ui_messages,
            "artifacts": runtime.list_artifacts_meta(),
            "latest_video": runtime.get_latest_video_meta(),
            "latest_screen": runtime.get_latest_screen_meta(),
            "images": runtime.list_images_meta(),
            "skills": runtime.get_skills_meta(),
            "system": runtime.get_system_meta(),
            "context_used": runtime.context_used_tokens(),
            "context_max": 1000000 if runtime.config.provider == "gemini" else int(runtime.config.context_size),
        }

    @app.get("/api/models/status")
    def api_models_status():
        return {
            "models": runtime.model_cache.status_summary(),
            "server_up": runtime.llm_server.is_up(),
            "provider": runtime.config.provider,
        }

    @app.post("/api/models/download")
    async def api_models_download():
        """Optional on-demand model download & local llama.cpp server startup."""
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: runtime.llm_server.start(force_restart=False, download_if_missing=True)
            )
            runtime.config.provider = "local"
            runtime.config.save_runtime_overrides(provider="local")
            return {
                "ok": True,
                "message": "Local model cached and llama.cpp server is online.",
                "system": runtime.get_system_meta(),
            }
        except Exception as e:
            return {
                "ok": False,
                "message": f"Local model setup failed: {e}",
                "system": runtime.get_system_meta(),
            }

    @app.post("/api/stop")
    def api_stop():
        runtime.agent.cancel_event.set()
        return {"ok": True}

    @app.post("/api/clear")
    def api_clear():
        runtime.agent.cancel_event.set()
        runtime.ui_messages.clear()
        runtime.llm_messages.clear()
        return api_state()

    @app.post("/api/upload")
    async def api_upload(files: List[UploadFile] = File(...)):
        saved = []
        for f in files:
            safe_name = Path(f.filename or f"upload_{int(time.time())}").name
            dest = runtime.config.uploads_dir / safe_name
            dest.write_bytes(await f.read())
            saved.append(str(dest))
        note = "Uploaded files to workspace: " + ", ".join(saved) + ". " if saved else ""
        return {"saved": saved, "note": note}

    @app.post("/api/export")
    def api_export():
        lines = [f"# Agent Chat Export - {datetime.datetime.now():%Y-%m-%d %H:%M}\n"]
        for m in runtime.llm_messages:
            if m.get("role") == "system":
                continue
            who = "User" if m["role"] == "user" else "Assistant"
            lines.append(f"## {who}\n\n{str(m.get('content', ''))[:8000]}\n")
        out = runtime.config.artifacts_dir / f"chat_export_{datetime.datetime.now():%Y%m%d_%H%M%S}.md"
        out.write_text("\n".join(lines), encoding="utf-8")
        return {"exported": out.name, "artifacts": runtime.list_artifacts_meta()}

    @app.post("/api/skills/github")
    async def api_skill_github(req: GithubSkillReq):
        if not req.repo_url.strip():
            return {"message": "Enter a GitHub URL first.", "skills": runtime.get_skills_meta()}
        loop = asyncio.get_running_loop()
        msg = await loop.run_in_executor(
            None, lambda: runtime.skill_manager.install_from_github(req.repo_url.strip(), req.subpath.strip())
        )
        return {"message": msg, "skills": runtime.get_skills_meta()}

    @app.post("/api/skills/upload")
    async def api_skill_upload(file: UploadFile = File(...)):
        safe_name = Path(file.filename or "SKILL.md").name
        tmp_path = Path(tempfile.gettempdir()) / f"skill_up_{int(time.time())}_{safe_name}"
        tmp_path.write_bytes(await file.read())
        try:
            msg = runtime.skill_manager.install_from_uploaded_file(str(tmp_path))
        finally:
            tmp_path.unlink(missing_ok=True)
        return {"message": msg, "skills": runtime.get_skills_meta()}

    @app.post("/api/screen/capture")
    async def api_screen_capture():
        loop = asyncio.get_running_loop()
        out = await loop.run_in_executor(None, lambda: runtime.desktop_operator.action("screenshot"))
        return {"message": out, "screen": runtime.get_latest_screen_meta()}

    @app.get("/api/vnc/status")
    async def api_vnc_status():
        loop = asyncio.get_running_loop()
        up = await loop.run_in_executor(None, runtime.desktop_operator.ensure_vnc_running)
        return {"vnc_up": up, "novnc_installed": os.path.isdir("/usr/share/novnc")}

    @app.get("/novnc/{file_path:path}")
    def serve_novnc(file_path: str):
        novnc_root = os.path.realpath("/usr/share/novnc")
        if not os.path.isdir(novnc_root):
            raise HTTPException(status_code=404, detail="noVNC is only available on Linux with ENABLE_VIRTUAL_DESKTOP=true.")
        rel = (file_path or "vnc_lite.html").lstrip("/")
        target = os.path.realpath(os.path.join(novnc_root, rel))
        if not (target == novnc_root or target.startswith(novnc_root + os.sep)) or not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="noVNC asset not found.")
        return FileResponse(target)

    @app.websocket("/novnc/websockify")
    @app.websocket("/websockify")
    async def vnc_websockify_proxy(websocket: WebSocket):
        requested = websocket.scope.get("subprotocols") or []
        subproto = "binary" if "binary" in requested else None
        await websocket.accept(subprotocol=subproto)
        loop = asyncio.get_running_loop()
        if not _tcp_open("127.0.0.1", 5900, timeout=0.4):
            await loop.run_in_executor(None, runtime.desktop_operator.ensure_vnc_running)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 5900)
        except Exception as e:
            await websocket.close(code=1011, reason=f"VNC offline: {e}")
            return

        async def ws_to_tcp():
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        break
                    data = msg.get("bytes")
                    if data is None and msg.get("text") is not None:
                        data = msg["text"].encode("utf-8")
                    if data:
                        writer.write(data)
                        await writer.drain()
            except Exception:
                pass
            finally:
                try:
                    writer.close()
                except Exception:
                    pass

        async def tcp_to_ws():
            try:
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await websocket.send_bytes(data)
            except Exception:
                pass
            finally:
                try:
                    await websocket.close()
                except Exception:
                    pass

        t1 = asyncio.create_task(ws_to_tcp())
        t2 = asyncio.create_task(tcp_to_ws())
        _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for p in pending:
            p.cancel()

    @app.post("/api/provider")
    def api_set_provider(req: ProviderReq):
        if req.gemini_api_key is not None and req.gemini_api_key.strip():
            runtime.config.gemini_api_key = req.gemini_api_key.strip()
        if req.gemini_model is not None and req.gemini_model.strip():
            runtime.config.gemini_model = req.gemini_model.strip()

        prov = (req.provider or "local").strip().lower()
        if prov == "gemini" and not (runtime.config.gemini_api_key or "").strip():
            return {"ok": False, "message": "Enter a Gemini API key first.", "system": runtime.get_system_meta()}

        runtime.config.provider = "gemini" if prov == "gemini" else "local"
        runtime.config.save_runtime_overrides(
            provider=runtime.config.provider,
            gemini_api_key=runtime.config.gemini_api_key or None,
            gemini_model=runtime.config.gemini_model,
        )
        msg = f"Switched AI provider to {runtime.config.provider.upper()}."
        if runtime.config.provider == "local" and not runtime.llm_server.is_up(timeout=0.5):
            msg += " (Local server offline — click DOWNLOAD & START LOCAL in System tab.)"
        return {"ok": True, "message": msg, "system": runtime.get_system_meta()}

    @app.post("/api/system/restart")
    async def api_system_restart():
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: runtime.llm_server.start(force_restart=True, download_if_missing=True)
            )
            return {"ok": True, "message": "LLM server restarted.", "system": runtime.get_system_meta()}
        except Exception as e:
            return {"ok": False, "message": f"Restart failed: {e}", "system": runtime.get_system_meta()}

    @app.get("/api/files/screen")
    def api_file_screen():
        if not runtime.config.latest_screen_path.is_file():
            raise HTTPException(status_code=404, detail="No screenshot captured yet.")
        return FileResponse(str(runtime.config.latest_screen_path), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.get("/api/files/artifact/{rel_path:path}")
    def api_file_artifact(rel_path: str, download: int = 0):
        root = os.path.realpath(str(runtime.config.artifacts_dir))
        target = os.path.realpath(os.path.join(root, rel_path))
        if not (target == root or target.startswith(root + os.sep)) or not os.path.isfile(target):
            raise HTTPException(status_code=404, detail="Artifact not found.")
        headers = {"Cache-Control": "no-cache"}
        if download:
            return FileResponse(target, filename=os.path.basename(target), headers=headers)
        return FileResponse(target, headers=headers)

    @app.post("/api/chat/stream")
    async def api_chat_stream(req: ChatReq, request: Request):
        user_msg = (req.message or "").strip()
        if not user_msg:
            raise HTTPException(status_code=400, detail="Empty message.")

        if not runtime.agent_lock.acquire(blocking=False):
            async def busy_stream():
                yield f"data: {json.dumps({'type': 'error', 'text': 'Agent is currently executing a turn. Click Abort first or wait for completion.'})}\n\n"

            return StreamingResponse(busy_stream(), media_type="text/event-stream")

        runtime.ui_messages.append({"role": "user", "content": user_msg, "ts": int(time.time())})
        ev_q: queue.Queue = queue.Queue()
        ctx_max = 1000000 if runtime.config.provider == "gemini" else int(runtime.config.context_size)

        def worker():
            tool_log, reply, stats, images = [], "", "", []
            try:
                for ev in runtime.agent.run_turn(
                    user_msg,
                    runtime.llm_messages,
                    max_steps=0 if int(req.max_steps) <= 0 else max(1, min(int(req.max_steps), 5000)),
                    think_mode=req.reasoning_mode if req.reasoning_mode in ("off", "brief", "deep") else "brief",
                    auto_vision=bool(req.auto_vision),
                ):
                    kind, payload = ev[0], ev[1]
                    if kind == "tool_start":
                        entry = {"kind": "start", "text": str(payload), "ts": time.time()}
                        tool_log.append(entry)
                        ev_q.put({"type": "tool_start", "entry": entry})
                    elif kind == "tool_end":
                        entry = {"kind": "end", "text": str(payload), "ts": time.time()}
                        tool_log.append(entry)
                        ev_q.put({"type": "tool_end", "entry": entry})
                    elif kind == "status":
                        entry = {"kind": "status", "text": str(payload), "ts": time.time()}
                        tool_log.append(entry)
                        ev_q.put({"type": "status", "entry": entry, "text": str(payload)})
                    elif kind == "heartbeat":
                        ev_q.put({"type": "heartbeat", "text": str(payload)})
                    elif kind == "assistant_stream":
                        reply = str(payload)
                        ev_q.put({"type": "stream", "text": reply})
                    elif kind == "stats":
                        stats = str(payload)
                        ev_q.put({
                            "type": "stats",
                            "text": stats,
                            "context_used": runtime.context_used_tokens(),
                            "context_max": ctx_max,
                        })
                    elif kind == "done":
                        reply = str(payload)
                        for im in runtime.agent.toolkit.turn_downloaded_images:
                            p = im.get("path", "")
                            if os.path.exists(p):
                                images.append({
                                    "name": os.path.basename(p),
                                    "path": p,
                                    "url": f"/api/files/artifact/images/{os.path.basename(p)}?t={int(time.time())}",
                                    "title": im.get("title", ""),
                                    "source_url": im.get("source_url", ""),
                                    "size": im.get("size", ""),
                                })
                        runtime.ui_messages.append({
                            "role": "assistant",
                            "content": reply,
                            "tool_log": tool_log,
                            "stats": stats,
                            "images": images,
                            "ts": int(time.time()),
                        })
                        ev_q.put({
                            "type": "done",
                            "text": reply,
                            "tool_log": tool_log,
                            "stats": stats,
                            "images": images,
                            "context_used": runtime.context_used_tokens(),
                            "context_max": ctx_max,
                        })
            except Exception as e:
                tb = traceback.format_exc()[-1200:]
                err_msg = f"{type(e).__name__}: {str(e)[:400]}"
                print(f"\n[Agent Exception] {err_msg}\n{tb}", flush=True)
                runtime.ui_messages.append({
                    "role": "assistant",
                    "content": (reply + "\n\n" if reply else "") + f"**Runtime Error:** `{err_msg}`",
                    "tool_log": tool_log,
                    "stats": stats,
                    "images": images,
                    "error": tb,
                    "ts": int(time.time()),
                })
                ev_q.put({"type": "error", "text": err_msg, "traceback": tb})
            finally:
                runtime.agent.cancel_event.set()
                runtime.agent_lock.release()
                ev_q.put(None)

        threading.Thread(target=worker, daemon=True).start()

        async def sse_gen():
            last_stream_push = 0.0
            try:
                while True:
                    if await request.is_disconnected():
                        runtime.agent.cancel_event.set()
                        break
                    try:
                        item = ev_q.get_nowait()
                    except queue.Empty:
                        await asyncio.sleep(0.025)
                        continue
                    if item is None:
                        break
                    if item.get("type") == "stream":
                        now = time.time()
                        if now - last_stream_push < 0.055:
                            continue
                        last_stream_push = now
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
            finally:
                runtime.agent.cancel_event.set()

        return StreamingResponse(
            sse_gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return app
