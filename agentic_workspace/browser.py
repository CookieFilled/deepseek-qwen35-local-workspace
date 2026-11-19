"""Dedicated Playwright Chromium worker for DOM automation, video rendering, and Perchance image generation."""

from __future__ import annotations

import base64
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx
from PIL import Image

from .config import WorkspaceConfig

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


def html_to_markdown(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        from markdownify import markdownify as md

        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "iframe", "template"]):
            t.decompose()
        out = md(str(soup), heading_style="ATX")
    except Exception:
        out = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


class HeadlessBrowserWorker:
    """Thread-isolated Playwright worker supporting indexed DOM actions, FFmpeg video rendering, and Perchance."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        self.cmd_q: queue.Queue | None = None
        self._lock = threading.Lock()
        self._perchance_ctx = None
        self._perchance_page = None

    def _ensure_thread(self) -> None:
        with self._lock:
            if self.cmd_q is None:
                self._start_locked()

    def _start_locked(self) -> None:
        self.cmd_q = queue.Queue()
        threading.Thread(target=self._run_loop, args=(self.cmd_q,), daemon=True).start()

    def _launch(self, p):
        use_headless = self.config.browser_headless and not self.config.enable_virtual_desktop
        browser = p.chromium.launch(
            headless=use_headless,
            env={**os.environ},
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--renderer-process-limit=1",
                "--js-flags=--max-old-space-size=384",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-features=TranslateUI,BlinkGenPropertyTrees",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=BROWSER_UA,
            ignore_https_errors=True,
        )
        page = context.new_page()

        def _route_filter(route):
            if route.request.resource_type in ("media", "font"):
                return route.abort()
            return route.continue_()

        try:
            page.route("**/*", _route_filter)
        except Exception:
            pass
        return browser, context, page

    def _run_loop(self, cmd_q: queue.Queue) -> None:
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser, context, page = self._launch(p)
                while True:
                    task, res_q = cmd_q.get()
                    try:
                        if page.is_closed():
                            page = context.new_page()
                        res_q.put({"ok": True, "data": self._handle(page, browser, task)})
                    except Exception as e:
                        msg = f"{type(e).__name__}: {e}"
                        if any(k in msg.lower() for k in ("closed", "crash", "disconnected")):
                            try:
                                try:
                                    browser.close()
                                except Exception:
                                    pass
                                self._perchance_ctx = None
                                self._perchance_page = None
                                browser, context, page = self._launch(p)
                                msg += " (browser was restarted - retry the action)"
                            except Exception as e2:
                                msg += f" (restart failed: {e2})"
                        res_q.put({"ok": False, "error": msg[:1500]})
        except Exception as boot_err:
            while True:
                _, res_q = cmd_q.get()
                res_q.put({
                    "ok": False,
                    "error": f"Playwright unavailable ({boot_err}). Install with: pip install playwright && playwright install chromium",
                })

    @staticmethod
    def _index_elements(page, max_items: int = 35) -> str:
        try:
            items = page.evaluate(
                """(maxItems) => {
                    document.querySelectorAll("[data-agent-idx]").forEach(e => e.removeAttribute("data-agent-idx"));
                    const els = Array.from(document.querySelectorAll(
                        'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="textbox"]'
                    )).filter(el => {
                        const r = el.getBoundingClientRect();
                        return r.width > 4 && r.height > 4 && r.bottom >= 0 && r.top <= window.innerHeight + 200;
                    });
                    const out = [];
                    let idx = 0;
                    for (const el of els) {
                        if (idx >= maxItems) break;
                        el.setAttribute("data-agent-idx", String(idx));
                        const tag = el.tagName.toLowerCase();
                        const tp = el.getAttribute("type") || "";
                        const lbl = (el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.innerText || el.value || "").replace(/\\s+/g, " ").trim().slice(0, 60);
                        out.push(`[${idx}] <${tag}${tp ? " type=" + tp : ""}> ${lbl}`);
                        idx++;
                    }
                    return out;
                }""",
                max_items,
            )
            return "\n".join(items) if items else "(no visible interactive elements)"
        except Exception:
            return "(element indexing unavailable)"

    @staticmethod
    def _resolve_selector(sel: str) -> str:
        s = str(sel or "").strip()
        if re.fullmatch(r"\[?\d+\]?", s):
            idx = s.strip("[]")
            return f'[data-agent-idx="{idx}"]'
        return s

    def _handle(self, page, browser, task: dict) -> str:
        latest_screen = str(self.config.latest_screen_path)
        act = task.get("action", "goto")
        url = task.get("url", "") or ""
        if url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
            url = "https://" + url

        if act in ("goto", "get_markdown"):
            if url:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1200)
            if act == "goto":
                page.screenshot(path=latest_screen)
            elems = self._index_elements(page)
            body = html_to_markdown(page.content())
            limit = 4500 if act == "goto" else 6000
            return (
                f"URL: {page.url}\nTitle: {page.title()}\n\n"
                f'[Interactive Elements (pass index like "0" as selector)]\n{elems}\n\n'
                f"[Page Content]\n{body[:limit]}"
            )
        if act == "click":
            sel = self._resolve_selector(task.get("selector", ""))
            page.click(sel, timeout=10000)
            page.wait_for_timeout(1200)
            page.screenshot(path=latest_screen)
            elems = self._index_elements(page)
            return f"Clicked '{sel}'. Current URL: {page.url} | Title: {page.title()}\n\n[Interactive Elements]\n{elems}"
        if act == "fill":
            sel = self._resolve_selector(task.get("selector", ""))
            page.fill(sel, task.get("text", ""), timeout=10000)
            if str(task.get("press_enter", "")).lower() in ("1", "true", "yes"):
                page.keyboard.press("Enter")
                page.wait_for_timeout(1500)
            page.screenshot(path=latest_screen)
            elems = self._index_elements(page)
            return f"Filled '{sel}'. Current URL: {page.url}\n\n[Interactive Elements]\n{elems}"
        if act == "scroll":
            direction = (task.get("text") or "down").lower()
            page.mouse.wheel(0, -700 if direction == "up" else 700)
            page.wait_for_timeout(600)
            page.screenshot(path=latest_screen)
            elems = self._index_elements(page)
            return f"Scrolled {direction}.\n\n[Interactive Elements]\n{elems}"
        if act == "eval_js":
            code = task.get("js_code", "")
            try:
                out = page.evaluate(code)
            except Exception:
                out = page.evaluate(f"() => {{ {code} }}")
            return f"JS Result: {json.dumps(out, default=str)[:8000]}"
        if act == "screenshot":
            out_path = task.get("output_path") or latest_screen
            os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
            page.screenshot(
                path=out_path,
                full_page=str(task.get("full_page", "")).lower() in ("1", "true", "yes"),
            )
            if os.path.abspath(out_path) != os.path.abspath(latest_screen):
                shutil.copy2(out_path, latest_screen)
            return f"Screenshot saved to {out_path}"
        if act == "render_video":
            return self._render_video(browser, task, url)
        if act == "perchance_image":
            return self._generate_perchance_image(browser, task)
        return (
            f"Unknown browser action: {act}. "
            "Use goto|get_markdown|click|fill|scroll|eval_js|screenshot|render_video|perchance_image."
        )

    def _render_video(self, browser, task: dict, url: str) -> str:
        out_mp4 = task.get("output_path") or str(self.config.artifacts_dir / "motion_video.mp4")
        fps = int(task.get("fps", 30))
        total = int(task.get("total_frames", 150))
        width = int(task.get("width", 1920)) // 2 * 2
        height = int(task.get("height", 1080)) // 2 * 2
        frames_dir = tempfile.mkdtemp(prefix="motion_frames_")
        v_ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
        try:
            v_page = v_ctx.new_page()
            v_page.goto(url, wait_until="load", timeout=30000)
            v_page.wait_for_timeout(500)
            if not v_page.evaluate("typeof window.seekToFrame === 'function'"):
                return "Render error: the page does not define window.seekToFrame(frame, totalFrames). Add it and retry."
            for i in range(total):
                v_page.evaluate("([f, t]) => window.seekToFrame(f, t)", [i, total])
                v_page.wait_for_timeout(10)
                v_page.screenshot(path=f"{frames_dir}/frame_{i:05d}.png")
            shutil.copy2(f"{frames_dir}/frame_{total // 2:05d}.png", str(self.config.latest_screen_path))
        finally:
            v_ctx.close()
        os.makedirs(os.path.dirname(out_mp4) or ".", exist_ok=True)
        ff = subprocess.run(
            [
                "ffmpeg", "-y", "-framerate", str(fps), "-i", f"{frames_dir}/frame_%05d.png",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-crf", "18", "-movflags", "+faststart", out_mp4,
            ],
            capture_output=True, text=True, timeout=900,
        )
        shutil.rmtree(frames_dir, ignore_errors=True)
        if ff.returncode != 0:
            return f"FFmpeg error: {ff.stderr[-1500:]}"
        return f"Rendered {total} frames @ {fps}fps -> {out_mp4} ({os.path.getsize(out_mp4)} bytes)"

    def _get_perchance_page(self, browser):
        if self._perchance_ctx is None or self._perchance_page is None or self._perchance_page.is_closed():
            self._perchance_ctx = browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=BROWSER_UA,
                ignore_https_errors=True,
            )
            self._perchance_page = self._perchance_ctx.new_page()
            self._perchance_page.goto(
                "https://perchance.org/ai-text-to-image-generator",
                wait_until="domcontentloaded",
                timeout=25000,
            )
            self._perchance_page.wait_for_timeout(3000)
        return self._perchance_page

    def _generate_perchance_image(self, browser, task: dict) -> str:
        prompt = (task.get("prompt") or "").strip()
        if not prompt:
            return "[Image Gen Error] Prompt cannot be empty."
        neg_prompt = (task.get("negative_prompt") or "blurry, low quality, distorted, deformed, watermark, censored").strip()
        shape = (task.get("shape") or "square").lower()
        allow_18 = bool(task.get("allow_18_plus", True))
        out_path = task.get("output_path")
        if not out_path:
            slug = re.sub(r"[^a-zA-Z0-9]+", "_", prompt.lower()).strip("_")[:28] or "gen"
            out_path = str(self.config.images_dir / f"perchance_{slug}_{int(time.time())}.png")

        try:
            page = self._get_perchance_page(browser)
        except Exception as nav_err:
            self._perchance_ctx = None
            self._perchance_page = None
            return f"[Image Gen Error] Could not load Perchance: {nav_err}"

        before_sigs = set()
        for f in page.frames:
            try:
                sigs = f.evaluate("() => Array.from(document.querySelectorAll('img')).map(i => i.src.slice(0, 120))")
                before_sigs.update(sigs or [])
            except Exception:
                continue

        configured = False
        for _ in range(8):
            for f in page.frames:
                try:
                    ok = f.evaluate(
                        """([p, neg, shp, allow18]) => {
                            for (const b of document.querySelectorAll('button')) {
                                const txt = (b.textContent || '').trim().toLowerCase();
                                if (allow18 && (/over 18|18\\+|i am 18|continue to page|enter/i.test(txt)) && !/generate/i.test(txt)) {
                                    try { b.click(); } catch (e) {}
                                }
                            }
                            const textareas = Array.from(document.querySelectorAll('textarea, input[type="text"]')).filter(
                                el => el.offsetParent !== null && !/search/i.test(el.placeholder || '')
                            );
                            const btn = Array.from(document.querySelectorAll('button')).find(
                                b => /generate/i.test(b.textContent || '') && !b.disabled && b.offsetParent !== null
                            );
                            if (!textareas.length || !btn) return false;

                            for (const cb of document.querySelectorAll('input[type="checkbox"]')) {
                                const labelText = (
                                    (cb.labels && cb.labels[0] ? cb.labels[0].textContent : '') + ' ' +
                                    (cb.parentElement ? cb.parentElement.textContent : '') + ' ' +
                                    (cb.id || '') + ' ' + (cb.name || '')
                                ).toLowerCase();
                                if (/18\\+|nsfw|adult|unfiltered/.test(labelText)) {
                                    if (cb.checked !== allow18) cb.click();
                                } else if (/safe|filter|censor/.test(labelText)) {
                                    if (cb.checked === allow18) cb.click();
                                } else if (/enhance|rewrite|brainstorm|auto.?prompt/.test(labelText)) {
                                    if (cb.checked) cb.click();
                                }
                            }

                            for (const sel of document.querySelectorAll('select')) {
                                const opts = Array.from(sel.options);
                                const hasNsfwOpt = opts.some(o => /18\\+|nsfw|safe|filter/i.test(o.text));
                                if (hasNsfwOpt) {
                                    const targetOpt = allow18
                                        ? opts.find(o => /18\\+|nsfw|allow|off|unfiltered|no filter/i.test(o.text))
                                        : opts.find(o => /safe|filter|sfw|on/i.test(o.text));
                                    if (targetOpt && sel.value !== targetOpt.value) {
                                        sel.value = targetOpt.value;
                                        sel.dispatchEvent(new Event('change', { bubbles: true }));
                                    }
                                }
                                const shapeOpt = opts.find(o => o.text.toLowerCase().includes(shp));
                                if (shapeOpt && sel.value !== shapeOpt.value) {
                                    sel.value = shapeOpt.value;
                                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                                }
                            }

                            const setVal = (el, val) => {
                                el.focus();
                                el.value = val;
                                el.dispatchEvent(new Event('input', { bubbles: true }));
                                el.dispatchEvent(new Event('change', { bubbles: true }));
                            };
                            setVal(textareas[0], p);
                            if (textareas.length > 1 && neg) setVal(textareas[1], neg);
                            btn.click();
                            return true;
                        }""",
                        [prompt, neg_prompt, shape, allow_18],
                    )
                    if ok:
                        configured = True
                        break
                except Exception:
                    continue
            if configured:
                break
            page.wait_for_timeout(1000)

        if not configured:
            page.screenshot(path=str(self.config.latest_screen_path))
            return "[Image Gen Error] Could not locate Perchance generator controls."

        raw_bytes = None
        turnstile_clicked = False
        for _ in range(18):
            page.wait_for_timeout(1000)
            for f in page.frames:
                if not turnstile_clicked and "challenges.cloudflare.com" in (f.url or ""):
                    try:
                        el = f.frame_element()
                        box = el.bounding_box()
                        if box:
                            page.mouse.click(box["x"] + 28, box["y"] + box["height"] / 2)
                            turnstile_clicked = True
                    except Exception:
                        pass
                try:
                    res = f.evaluate(
                        """async (oldSigs) => {
                            const oldSet = new Set(oldSigs || []);
                            const imgs = Array.from(document.querySelectorAll('img')).filter(
                                i => i.complete && i.naturalWidth >= 256 && i.naturalHeight >= 256 && !oldSet.has(i.src.slice(0, 120))
                            );
                            if (imgs.length) {
                                const last = imgs[imgs.length - 1];
                                try {
                                    const resp = await fetch(last.src);
                                    const blob = await resp.blob();
                                    return await new Promise((resolve) => {
                                        const reader = new FileReader();
                                        reader.onloadend = () => resolve(reader.result);
                                        reader.readAsDataURL(blob);
                                    });
                                } catch (e) {
                                    return "URL|" + last.src;
                                }
                            }
                            const canvases = Array.from(document.querySelectorAll('canvas')).filter(
                                c => c.width >= 256 && c.height >= 256
                            );
                            if (canvases.length) {
                                try { return canvases[canvases.length - 1].toDataURL('image/png'); } catch (e) {}
                            }
                            return null;
                        }""",
                        list(before_sigs),
                    )
                    if isinstance(res, str):
                        if res.startswith("data:image/") and "," in res:
                            raw_bytes = base64.b64decode(res.split(",", 1)[1])
                            break
                        if res.startswith("URL|http"):
                            img_url = res.split("|", 1)[1]
                            with httpx.Client(timeout=20.0, follow_redirects=True, verify=False, headers={"User-Agent": BROWSER_UA}) as hc:
                                resp = hc.get(img_url)
                            if resp.status_code == 200 and len(resp.content) > 2000:
                                raw_bytes = resp.content
                                break
                except Exception:
                    continue
            if raw_bytes:
                break

        page.screenshot(path=str(self.config.latest_screen_path))
        if not raw_bytes:
            return f"[Image Gen Error] Perchance did not return an image within 18s (check Viewport tab {self.config.latest_screen_path})."

        im = Image.open(io.BytesIO(raw_bytes))
        im.load()
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        Path(out_path).write_bytes(raw_bytes)
        return f"OK|{out_path}|{im.width}x{im.height}"

    def execute(self, task: dict, timeout: int = 120) -> str:
        self._ensure_thread()
        res_q: queue.Queue = queue.Queue()
        assert self.cmd_q is not None
        self.cmd_q.put((task, res_q))
        try:
            res = res_q.get(timeout=timeout)
        except queue.Empty:
            with self._lock:
                self._start_locked()
            return f"[Browser Error] '{task.get('action')}' timed out after {timeout}s; the browser was reset."
        return res["data"] if res["ok"] else f"[Browser Error] {res['error']}"
