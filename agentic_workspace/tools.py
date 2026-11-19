"""Agent tools: shell, file ops, uncensored web/image search, image generation, browser, desktop, vision, and skills."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import platform
import re
import signal
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path
from typing import Callable, Optional

import httpx
from PIL import Image

from .browser import BROWSER_UA, HeadlessBrowserWorker, html_to_markdown
from .config import WorkspaceConfig
from .desktop import DesktopOperator
from .skills import SkillManager

INT_PARAMS = {"timeout", "max_results", "download_top_n", "x", "y", "fps", "total_frames", "width", "height", "max_chars"}
BOOL_PARAMS = {"full_page", "press_enter", "allow_18_plus"}


def truncate_output(text: str, max_chars: int = 10000) -> str:
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    return (
        text[:half]
        + f"\n\n... [TRUNCATED {len(text) - max_chars} CHARS TO PROTECT CONTEXT] ...\n\n"
        + text[-half:]
    )


def encode_image_data_url(img_path: str, max_side: int = 960, draw_grid: bool = False) -> str:
    im = Image.open(img_path).convert("RGB")
    if draw_grid and im.width >= 1000:
        from PIL import ImageDraw

        draw = ImageDraw.Draw(im)
        for gx in range(200, im.width, 200):
            draw.line([(gx, 0), (gx, im.height)], fill=(0, 220, 255), width=1)
            draw.rectangle([gx + 2, 2, gx + 42, 18], fill=(0, 0, 0))
            draw.text((gx + 4, 4), f"x{gx}", fill=(0, 240, 255))
        for gy in range(200, im.height, 200):
            draw.line([(0, gy), (im.width, gy)], fill=(0, 220, 255), width=1)
            draw.rectangle([2, gy + 2, 42, gy + 18], fill=(0, 0, 0))
            draw.text((4, gy + 4), f"y{gy}", fill=(0, 240, 255))
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _scrape_uncensored_web_fallbacks(query: str, max_results: int = 8) -> list[dict]:
    from bs4 import BeautifulSoup

    out: list[dict] = []
    seen_urls: set[str] = set()
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "kp=-2; kl=wt-wt; safesearch=off",
    }
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers, verify=False) as client_http:
        try:
            resp = client_http.post(
                "https://html.duckduckgo.com/html/",
                data={"q": query, "kp": "-2", "kl": "wt-wt"},
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for div in soup.select(".result"):
                    a = div.select_one(".result__a")
                    snip = div.select_one(".result__snippet")
                    if not a:
                        continue
                    href = a.get("href", "")
                    if "uddg=" in href:
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
                        href = qs.get("uddg", [href])[0]
                    if href.startswith("http") and href not in seen_urls:
                        seen_urls.add(href)
                        out.append({
                            "title": a.get_text(" ", strip=True),
                            "href": href,
                            "body": snip.get_text(" ", strip=True) if snip else "",
                        })
                        if len(out) >= max_results:
                            return out
        except Exception:
            pass

        try:
            q_enc = urllib.parse.quote_plus(query)
            resp = client_http.get(
                f"https://search.brave.com/search?q={q_enc}&source=web&safesearch=off",
                cookies={"safesearch": "off"},
            )
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for div in soup.select("div.snippet[data-type='web'], div.snippet"):
                    a = div.select_one("a.svelte-14r20fy, a[href^='http']")
                    if not a:
                        continue
                    href = a.get("href", "")
                    if not href.startswith("http") or "brave.com" in href or href in seen_urls:
                        continue
                    title_el = div.select_one(".title, .snippet-title") or a
                    desc_el = div.select_one(".snippet-description, .snippet-content")
                    seen_urls.add(href)
                    out.append({
                        "title": title_el.get_text(" ", strip=True),
                        "href": href,
                        "body": desc_el.get_text(" ", strip=True) if desc_el else "",
                    })
                    if len(out) >= max_results:
                        return out
        except Exception:
            pass
    return out


def _scrape_uncensored_image_fallbacks(query: str, max_results: int = 12) -> list[dict]:
    out: list[dict] = []
    q_enc = urllib.parse.quote_plus(query)
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cookie": "SRCHHPGUSR=ADLT=OFF; kp=-2",
    }
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers, verify=False) as c:
        try:
            r_vqd = c.post("https://duckduckgo.com/", data={"q": query, "kp": "-2"})
            vqd_m = re.search(r"vqd=['\"]([\d-]+)['\"]", r_vqd.text)
            if vqd_m:
                vqd = vqd_m.group(1)
                r_img = c.get(
                    f"https://duckduckgo.com/i.js?l=wt-wt&o=json&q={q_enc}&vqd={vqd}&f=,,,,,&p=-1",
                    headers={**headers, "Referer": "https://duckduckgo.com/"},
                )
                if r_img.status_code == 200:
                    for item in r_img.json().get("results", []):
                        out.append({
                            "title": item.get("title") or query,
                            "image": item.get("image"),
                            "thumbnail": item.get("thumbnail"),
                            "url": item.get("url") or "",
                        })
                        if len(out) >= max_results:
                            return out
        except Exception:
            pass

        try:
            r_bing = c.get(f"https://www.bing.com/images/search?q={q_enc}&adlt=off&safeSearch=Off&form=HDRSC2")
            if r_bing.status_code == 200:
                for m in re.finditer(r"m=\{(?:&quot;|[^}])+\}", r_bing.text):
                    raw = m.group(0)[2:].replace("&quot;", '"')
                    try:
                        meta = json.loads(raw)
                        murl = meta.get("murl")
                        turl = meta.get("turl")
                        if murl:
                            out.append({
                                "title": meta.get("t") or meta.get("desc") or query,
                                "image": murl,
                                "thumbnail": turl,
                                "url": meta.get("purl") or murl,
                            })
                            if len(out) >= max_results:
                                return out
                    except Exception:
                        continue
        except Exception:
            pass
    return out


class AgentToolkit:
    """Encapsulates all 11 agent tools with cross-platform local-PC execution."""

    def __init__(
        self,
        config: WorkspaceConfig,
        skill_manager: SkillManager,
        browser_worker: HeadlessBrowserWorker,
        desktop_operator: DesktopOperator,
        vision_fn: Callable[[str, str], str],
        log_event_fn: Callable[[str], None],
    ) -> None:
        self.config = config
        self.skill_manager = skill_manager
        self.browser_worker = browser_worker
        self.desktop_operator = desktop_operator
        self.vision_fn = vision_fn
        self.log_event = log_event_fn
        self.turn_downloaded_images: list[dict] = []

    def tools_schema(self) -> list[dict]:
        ws = self.config.workspace_dir.as_posix()
        imgs = self.config.images_dir.as_posix()
        arts = self.config.artifacts_dir.as_posix()
        shell_desc = (
            f"Run a shell command in {ws} (non-interactive, no stdin). "
            + ("Uses PowerShell/cmd on Windows." if platform.system() == "Windows" else "Background long jobs with: nohup cmd > /tmp/x.log 2>&1 &")
        )
        return [
            {
                "name": "web_search",
                "description": "Uncensored web search (safesearch=off) for URLs, docs, news, and unfiltered facts.",
                "parameters": {"query": "string (required)", "max_results": "integer (default 8)"},
            },
            {
                "name": "fetch_url",
                "description": "Fast HTTP fetch of a web page/API as clean markdown (no browser). Prefer this for reading articles/docs.",
                "parameters": {"url": "string (required)", "max_chars": "integer (default 9000)"},
            },
            {
                "name": "image_search",
                "description": f"Uncensored image search (safesearch=off), downloads & verifies top images into {imgs}/ and returns local paths.",
                "parameters": {"query": "string (required)", "max_results": "integer (default 8)", "download_top_n": "integer (default 4)"},
            },
            {
                "name": "generate_image",
                "description": f"Generate an uncensored 18+ AI image from a verbatim text prompt via Perchance and save it to {imgs}/.",
                "parameters": {
                    "prompt": "string (required - exact visual prompt)",
                    "negative_prompt": "string (optional)",
                    "shape": "square|portrait|landscape (default square)",
                    "allow_18_plus": "bool (default true)",
                },
            },
            {
                "name": "view_image",
                "description": "Look at a local image (or the latest browser/desktop screenshot if path is omitted) with the vision model and get a text answer.",
                "parameters": {"path": "string (optional)", "question": "string (optional)"},
            },
            {
                "name": "run_bash",
                "description": shell_desc,
                "parameters": {"command": "string (required)", "timeout": "integer seconds (default 180)"},
            },
            {
                "name": "file_ops",
                "description": f"Read/write/append/edit/list files. Relative paths are inside {ws}. Save deliverables to {arts}/.",
                "parameters": {
                    "action": "read|write|append|edit|list",
                    "path": "string",
                    "content": "string (write/append)",
                    "old_text": "string (edit)",
                    "new_text": "string (edit)",
                },
            },
            {
                "name": "browser_action",
                "description": "Headless Chromium: goto, get_markdown, click, fill, scroll, eval_js, screenshot, render_video (deterministic MP4 via window.seekToFrame).",
                "parameters": {
                    "action": "goto|get_markdown|click|fill|scroll|eval_js|screenshot|render_video",
                    "url": "string",
                    "selector": "string",
                    "text": "string",
                    "js_code": "string",
                    "output_path": "string",
                    "fps": "integer",
                    "total_frames": "integer",
                    "width": "integer",
                    "height": "integer",
                },
            },
            {
                "name": "desktop_action",
                "description": "Control local PC desktop (or Linux Xvfb virtual display): screenshot, click, double_click, right_click, scroll, type, key.",
                "parameters": {
                    "action": "screenshot|click|double_click|right_click|scroll|type|key",
                    "x": "integer",
                    "y": "integer",
                    "text": "string",
                    "key": "string",
                },
            },
            {
                "name": "activate_skill",
                "description": "Load the full instructions of an installed SKILL.md skill (do this first for matching tasks).",
                "parameters": {"skill_name": "string (required)"},
            },
            {
                "name": "install_skill",
                "description": "Install a SKILL.md skill from a GitHub repo/folder URL.",
                "parameters": {"repo_url": "string (required)", "subpath": "string (optional)"},
            },
        ]

    @staticmethod
    def coerce_args(args: dict) -> dict:
        out = {}
        for k, v in (args or {}).items():
            if k in INT_PARAMS:
                try:
                    out[k] = int(float(v))
                except Exception:
                    continue
            elif k in BOOL_PARAMS:
                out[k] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "y")
            elif isinstance(v, str):
                out[k] = v
            elif isinstance(v, (dict, list)):
                out[k] = json.dumps(v, ensure_ascii=False)
            else:
                out[k] = str(v)
        return out

    def run_bash(self, command: str, timeout: int = 180) -> str:
        if not (command or "").strip():
            return "[Shell Error] Empty command."
        try:
            timeout = max(1, min(int(timeout), 1800))
        except Exception:
            timeout = 180

        env = {
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
        }
        if self.desktop_operator.use_xvfb:
            env["DISPLAY"] = ":99"

        is_win = platform.system() == "Windows"
        if is_win:
            cmd_vec = ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
            popen_extra = {}
        else:
            cmd_vec = ["/bin/bash", "-c", command]
            popen_extra = {"start_new_session": True}

        try:
            with tempfile.TemporaryFile() as fo, tempfile.TemporaryFile() as fe:
                proc = subprocess.Popen(
                    cmd_vec,
                    cwd=str(self.config.workspace_dir),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=fo,
                    stderr=fe,
                    **popen_extra,
                )
                timed_out = False
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    try:
                        if not is_win:
                            os.killpg(proc.pid, signal.SIGKILL)
                        else:
                            proc.kill()
                    except Exception:
                        proc.kill()
                    try:
                        proc.wait(timeout=5)
                    except Exception:
                        pass
                fo.seek(0)
                fe.seek(0)
                out = fo.read().decode("utf-8", "replace")
                err = fe.read().decode("utf-8", "replace")
            text = out
            if err:
                text += ("\n[STDERR]\n" if text else "[STDERR]\n") + err
            if not text.strip():
                text = "(no output)"
            if timed_out:
                return truncate_output(
                    f"[Timeout] Command exceeded {timeout}s and was killed.\n--- partial output ---\n{text}",
                    8000,
                )
            return truncate_output(f"[Exit Code: {proc.returncode}]\n{text}", 8000)
        except Exception as e:
            return f"[Shell Error] {type(e).__name__}: {e}"

    def file_ops(self, action: str, path: str = ".", content: str = "", old_text: str = "", new_text: str = "") -> str:
        p = Path(path) if os.path.isabs(path) else self.config.workspace_dir / path
        try:
            if action == "read":
                if not p.exists():
                    return f"File not found: {p}"
                if p.is_dir():
                    return f"{p} is a directory - use action=list."
                return truncate_output(p.read_text(encoding="utf-8", errors="replace"), 12000)
            if action in ("write", "append"):
                p.parent.mkdir(parents=True, exist_ok=True)
                if action == "write":
                    p.write_text(content, encoding="utf-8")
                else:
                    with open(p, "a", encoding="utf-8") as f:
                        f.write(content)
                return f"{'Wrote' if action == 'write' else 'Appended'} {len(content.encode('utf-8'))} bytes to {p}"
            if action == "edit":
                if not p.exists():
                    return f"File not found: {p}"
                src = p.read_text(encoding="utf-8")
                if not old_text or old_text not in src:
                    return f"Could not find exact old_text match in {p}. Read the file first and copy the text exactly."
                n = src.count(old_text)
                p.write_text(src.replace(old_text, new_text, 1), encoding="utf-8")
                return f"Updated {p} (replaced first of {n} match{'es' if n != 1 else ''})."
            if action == "list":
                target = p if p.exists() else self.config.workspace_dir
                items = []
                base_depth = len(target.parts)
                for root, dirs, files in os.walk(target):
                    dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
                    if len(Path(root).parts) - base_depth >= 3:
                        dirs[:] = []
                    for fn in sorted(files):
                        fp = Path(root) / fn
                        try:
                            items.append(f"{os.path.relpath(fp, target)} ({fp.stat().st_size} B)")
                        except OSError:
                            pass
                    if len(items) > 150:
                        items.append("... (truncated)")
                        break
                return f"[{target}]\n" + ("\n".join(items) if items else "(empty)")
            return f"Unknown file_ops action: {action}. Use read|write|append|edit|list."
        except Exception as e:
            return f"[File Error] {type(e).__name__}: {e}"

    def web_search(self, query: str, max_results: int = 8) -> str:
        max_results = max(1, min(int(max_results or 8), 20))
        results: list[dict] = []
        seen_hrefs: set[str] = set()
        last_err: Optional[Exception] = None

        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore

            for backend in ("auto", "html", "lite"):
                try:
                    with DDGS() as ddgs:
                        try:
                            batch = list(ddgs.text(query, region="wt-wt", safesearch="off", backend=backend, max_results=max_results))
                        except TypeError:
                            batch = list(ddgs.text(query, region="wt-wt", safesearch="off", max_results=max_results))
                    for r in batch:
                        h = r.get("href") or r.get("url") or ""
                        if h and h not in seen_hrefs:
                            seen_hrefs.add(h)
                            results.append({"title": r.get("title", ""), "href": h, "body": r.get("body", "")})
                    if len(results) >= max_results:
                        break
                except Exception as e:
                    last_err = e
        except Exception as e:
            last_err = e

        if len(results) < max_results:
            for r in _scrape_uncensored_web_fallbacks(query, max_results=max_results):
                if r["href"] not in seen_hrefs:
                    seen_hrefs.add(r["href"])
                    results.append(r)
                if len(results) >= max_results:
                    break

        if not results:
            return f"No search results found for '{query}'." + (f" (Last backend note: {last_err})" if last_err else "")
        return "\n\n".join(
            f"{i}. **{r.get('title')}**\n   URL: {r.get('href')}\n   {r.get('body')}"
            for i, r in enumerate(results[:max_results], 1)
        )

    def fetch_url(self, url: str, max_chars: int = 6000) -> str:
        try:
            if not re.match(r"^https?://", url or ""):
                url = "https://" + (url or "")
            with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": BROWSER_UA}) as c:
                r = c.get(url)
            ctype = r.headers.get("content-type", "").lower()
            if "html" in ctype or "xml" in ctype or not ctype:
                body = html_to_markdown(r.text)
            elif "json" in ctype or ctype.startswith("text/"):
                body = r.text
            else:
                return f"[Fetch] {r.url} returned non-text content ({ctype}, {len(r.content)} bytes)."
            return truncate_output(f"URL: {r.url}\nStatus: {r.status_code}\n\n{body}", max(1000, min(int(max_chars), 20000)))
        except Exception as e:
            return f"[Fetch Error] {type(e).__name__}: {e}"

    def image_search(self, query: str, max_results: int = 8, download_top_n: int = 4) -> str:
        max_results = max(1, min(int(max_results or 8), 20))
        download_top_n = max(1, min(int(download_top_n or 4), 10))
        results: list[dict] = []
        seen_imgs: set[str] = set()
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore

            with DDGS() as ddgs:
                raw_ddg = list(ddgs.images(query, region="wt-wt", safesearch="off", max_results=max(max_results * 2, 16)))
            for r in raw_ddg:
                u = r.get("image") or r.get("thumbnail")
                if u and u not in seen_imgs:
                    seen_imgs.add(u)
                    results.append(r)
        except Exception:
            pass

        if len(results) < max_results:
            for r in _scrape_uncensored_image_fallbacks(query, max_results=max_results * 2):
                u = r.get("image") or r.get("thumbnail")
                if u and u not in seen_imgs:
                    seen_imgs.add(u)
                    results.append(r)

        if not results:
            return f"No image results found for '{query}'."

        self.config.images_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", query.lower()).strip("_")[:28] or "img"
        entries, got = [], 0

        with httpx.Client(
            timeout=12.0,
            follow_redirects=True,
            verify=False,
            headers={"User-Agent": BROWSER_UA, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"},
        ) as http_c:
            for idx, r in enumerate(results, 1):
                if len(entries) >= max_results and got >= download_top_n:
                    break
                title = r.get("title") or f"Image {idx}"
                source_url = r.get("url") or ""
                img_url = r.get("image") or ""
                local = "(not downloaded)"
                if got < download_top_n:
                    for cand in (r.get("image"), r.get("thumbnail")):
                        if not cand or not str(cand).startswith("http"):
                            continue
                        try:
                            parsed_origin = urllib.parse.urlparse(source_url or cand)
                            origin_ref = (
                                f"{parsed_origin.scheme}://{parsed_origin.netloc}/"
                                if parsed_origin.netloc
                                else "https://duckduckgo.com/"
                            )
                            resp = http_c.get(cand, headers={"Referer": origin_ref, "User-Agent": BROWSER_UA})
                            if resp.status_code != 200 or not (1500 < len(resp.content) < 25_000_000):
                                continue
                            im = Image.open(io.BytesIO(resp.content)).convert("RGB")
                            if max(im.size) > 1600:
                                im.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                            h = hashlib.md5(cand.encode()).hexdigest()[:6]
                            out_file = self.config.images_dir / f"{slug}_{got + 1}_{h}.jpg"
                            im.save(out_file, format="JPEG", quality=90)
                            local, img_url = str(out_file), cand
                            got += 1
                            self.turn_downloaded_images.append({
                                "path": local,
                                "title": title,
                                "source_url": source_url,
                                "image_url": cand,
                                "size": f"{im.width}x{im.height}",
                            })
                            break
                        except Exception:
                            continue
                if len(entries) < max_results or local != "(not downloaded)":
                    entries.append(
                        f"{len(entries) + 1}. **{title}**\n"
                        f"   - Local Saved Path: `{local}`\n"
                        f"   - Web Image URL: {img_url}\n"
                        f"   - Source Page: {source_url}"
                    )
        return (
            f"Found {len(results)} uncensored images for '{query}' "
            f"(downloaded & verified {got} into `{self.config.images_dir}`):\n\n"
            + "\n\n".join(entries[:max_results])
        )

    def generate_image(self, prompt: str, negative_prompt: str = "", shape: str = "square", allow_18_plus: bool = True) -> str:
        prompt_clean = (prompt or "").strip()
        if not prompt_clean:
            return "[Image Gen Error] Prompt cannot be empty."

        res = self.browser_worker.execute(
            {
                "action": "perchance_image",
                "prompt": prompt_clean,
                "negative_prompt": negative_prompt,
                "shape": shape,
                "allow_18_plus": allow_18_plus,
            },
            timeout=32,
        )

        if res.startswith("OK|"):
            _, out_path, dims = res.split("|", 2)
            self.turn_downloaded_images.append({
                "path": out_path,
                "title": f"Generated (18+): {prompt_clean[:60]}",
                "source_url": "https://perchance.org/ai-text-to-image-generator",
                "image_url": out_path,
                "size": dims,
            })
            return f"Generated image ({dims}, 18+={allow_18_plus}) via Perchance -> `{out_path}`."

        dims_map = {"portrait": (768, 1152), "landscape": (1152, 768), "square": (1024, 1024)}
        w, h = dims_map.get((shape or "square").lower(), (1024, 1024))
        enc = urllib.parse.quote(prompt_clean)
        safe_flag = "false" if allow_18_plus else "true"
        fb_url = (
            f"https://image.pollinations.ai/prompt/{enc}"
            f"?model=flux&width={w}&height={h + 48}&safe={safe_flag}&enhance=false&nologo=true&seed={int(time.time())}"
        )
        try:
            with httpx.Client(timeout=45.0, follow_redirects=True, verify=False, headers={"User-Agent": BROWSER_UA}) as hc:
                r = hc.get(fb_url)
            if r.status_code == 200 and len(r.content) > 2000:
                im = Image.open(io.BytesIO(r.content)).convert("RGB")
                if im.height > 48:
                    im = im.crop((0, 0, im.width, im.height - 48))
                slug = re.sub(r"[^a-zA-Z0-9]+", "_", prompt_clean.lower()).strip("_")[:28] or "gen"
                out_path = str(self.config.images_dir / f"gen_{slug}_{int(time.time())}.png")
                im.save(out_path, format="PNG")
                self.turn_downloaded_images.append({
                    "path": out_path,
                    "title": f"Generated (18+): {prompt_clean[:60]}",
                    "source_url": fb_url,
                    "image_url": out_path,
                    "size": f"{im.width}x{im.height}",
                })
                return f"Generated image ({im.width}x{im.height}, 18+={allow_18_plus}) -> `{out_path}`."
            return f"{res} | Fallback failed with HTTP {r.status_code}"
        except Exception as fb_err:
            return f"{res} | Fallback also failed: {type(fb_err).__name__}: {fb_err}"

    def dispatch(self, name: str, args: dict) -> str:
        try:
            a = self.coerce_args(args)
            self.log_event(f"Tool call: {name}({json.dumps(a, default=str)[:140]})")
            if name == "run_bash":
                return self.run_bash(a.get("command", ""), a.get("timeout", 180))
            if name == "file_ops":
                return self.file_ops(a.get("action", "list"), a.get("path", "."), a.get("content", ""), a.get("old_text", ""), a.get("new_text", ""))
            if name == "browser_action":
                long_job = a.get("action") == "render_video"
                return self.browser_worker.execute(a, timeout=1500 if long_job else 120)
            if name == "desktop_action":
                return self.desktop_operator.action(a.get("action", "screenshot"), a.get("x", 0), a.get("y", 0), a.get("text", ""), a.get("key", ""))
            if name == "web_search":
                return self.web_search(a.get("query", ""), a.get("max_results", 8))
            if name == "fetch_url":
                return self.fetch_url(a.get("url", ""), a.get("max_chars", 9000))
            if name == "image_search":
                return self.image_search(a.get("query", ""), a.get("max_results", 8), a.get("download_top_n", 4))
            if name == "generate_image":
                return self.generate_image(a.get("prompt", ""), a.get("negative_prompt", ""), a.get("shape", "square"), a.get("allow_18_plus", True))
            if name == "view_image":
                return self.vision_fn(a.get("path", ""), a.get("question", ""))
            if name == "activate_skill":
                return self.skill_manager.activate_skill(a.get("skill_name", ""))
            if name == "install_skill":
                return self.skill_manager.install_from_github(a.get("repo_url", ""), a.get("subpath", ""))
            tool_names = [t["name"] for t in self.tools_schema()]
            return f"Unknown tool: '{name}'. Available tools: {', '.join(tool_names)}"
        except Exception as e:
            self.log_event(f"Tool error in {name}: {type(e).__name__}: {e}")
            return f"[Tool Error] {name} failed: {type(e).__name__}: {e}"
