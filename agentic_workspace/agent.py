"""Autonomous streaming agent loop with rolling context compaction, dual Local/Gemini providers, and vision."""

from __future__ import annotations

import datetime
import json
import os
import platform
import queue
import re
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait

from openai import OpenAI

from .browser import HeadlessBrowserWorker
from .config import WorkspaceConfig
from .desktop import DesktopOperator
from .llm_server import LlamaServerManager
from .skills import SkillManager
from .tools import AgentToolkit, encode_image_data_url, truncate_output

STOP_SEQS = ["<|im_end|>", "<|endoftext|>", "<tool_response>", "<|im_start|>"]
THINK_BUDGETS = {"off": 0, "brief": 3500, "deep": 10000}
_BIG_PARAM = re.compile(r"(<parameter=(?:content|new_text|old_text|js_code|command|text)>)(.*?)(</parameter>)", re.S)


def strip_think(t: str) -> str:
    t = re.sub(r"<think>.*?</think>", "", t, flags=re.S)
    if "<think>" in t:
        t = t.split("<think>", 1)[0]
    if "</think>" in t:
        t = t.split("</think>")[-1]
    return t


def _in_think(t: str) -> bool:
    return t.rfind("<think>") > t.rfind("</think>")


def _think_len(t: str) -> int:
    return len(t) - t.rfind("<think>") if _in_think(t) else 0


def _loads_lenient(raw: str):
    raw = raw.strip()
    for cand in (raw, raw + "}" * max(0, raw.count("{") - raw.count("}")), re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            return json.loads(cand, strict=False)
        except Exception:
            continue
    return None


def _parse_params(inner: str) -> dict:
    args = {}
    for pm in re.finditer(r"<parameter=\s*([\w\-.]+)\s*>(.*?)(?:</parameter>|(?=<parameter=)|\Z)", inner, re.S):
        v = pm.group(2)
        if v.startswith("\n"):
            v = v[1:]
        if v.endswith("\n"):
            v = v[:-1]
        args[pm.group(1).strip()] = v
    return args


def parse_tool_calls(text: str) -> list[tuple[str, dict]]:
    calls: list[tuple[str, dict]] = []
    try:
        for m in re.finditer(r"<tool_call>(.*?)(?:</tool_call>|\Z)", text, re.S):
            body = m.group(1)
            fm = re.search(r"<function=\s*([\w\-.]+)\s*>(.*?)(?:</function>|\Z)", body, re.S)
            if fm:
                calls.append((fm.group(1).strip(), _parse_params(fm.group(2))))
                continue
            obj = _loads_lenient(body)
            if isinstance(obj, dict) and "name" in obj:
                args = obj.get("arguments", obj.get("parameters", {}))
                if isinstance(args, str):
                    args = _loads_lenient(args) or {}
                calls.append((str(obj["name"]), args if isinstance(args, dict) else {}))
        if not calls:
            for fm in re.finditer(r"<function=\s*([\w\-.]+)\s*>(.*?)(?:</function>|\Z)", text, re.S):
                calls.append((fm.group(1).strip(), _parse_params(fm.group(2))))
    except Exception:
        pass
    return calls


def _strip_markup(text: str) -> str:
    out = re.sub(r"<tool_call>.*?(?:</tool_call>|\Z)", "", text, flags=re.S)
    out = re.sub(r"<function=.*?(?:</function>|\Z)", "", out, flags=re.S)
    for tok in ("<|im_end|>", "<|endoftext|>", "<|im_start|>"):
        out = out.replace(tok, "")
    return out.strip()


def render_display(text: str, final: bool = False) -> str:
    def fmt(m):
        inner = m.group(1).strip()
        return f"<details><summary>Reasoning</summary>\n\n{inner}\n\n</details>\n\n" if inner else ""

    out = re.sub(r"<think>(.*?)</think>", fmt, text, flags=re.S)
    if "</think>" in out and "<think>" not in out:
        head, tail = out.split("</think>", 1)
        out = (f"<details><summary>Reasoning</summary>\n\n{head.strip()}\n\n</details>\n\n" if head.strip() else "") + tail
    if "<think>" in out:
        before, thinking = out.rsplit("<think>", 1)
        preview = re.sub(r"\s+", " ", thinking).strip()[-320:].replace("`", "'")
        out = before + f"\n*Thinking...* `{preview}`"
    names = re.findall(r"<function=\s*([\w\-.]+)", out)
    out = _strip_markup(out)
    if names and not final:
        out += f"\n\n*Preparing `{names[-1]}` call...*"
    return out.strip()


def est_tokens(s: str) -> int:
    return int(len(s) / 3.3) + 6


def _msg_text(m: dict) -> str:
    c = m.get("content", "")
    if isinstance(c, list):
        return "\n".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return str(c)


def render_chatml(messages: list[dict]) -> str:
    return "".join(f"<|im_start|>{m.get('role', 'user')}\n{_msg_text(m)}<|im_end|>\n" for m in messages)


def _shrink_params(t: str, keep: int = 160) -> str:
    def f(m):
        v = m.group(2)
        return m.group(1) + (v if len(v) <= keep * 2 else v[:keep] + f"\n...[{len(v) - keep} chars omitted]...") + m.group(3)

    return _BIG_PARAM.sub(f, t)


def _head(t: str, n: int) -> str:
    return t if len(t) <= n else t[:n] + "\n...[older output trimmed to save context]..."


def _is_looping(text: str) -> bool:
    if len(text) < 900:
        return False
    tail = text[-1600:]
    unit = tail[-90:]
    return len(set(unit)) >= 6 and tail.count(unit) >= 5


def _trim_loop(text: str) -> str:
    unit = text[-90:]
    idx = text.find(unit)
    return text[: idx + len(unit)] if idx >= 0 else text


def _client_text(msg) -> str:
    text = getattr(msg, "content", None) or ""
    if not text:
        text = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None) or ""
    return text


class AgentEngine:
    """Core autonomous execution engine with Local llama.cpp and Gemini API support."""

    def __init__(
        self,
        config: WorkspaceConfig,
        llm_server: LlamaServerManager,
        skill_manager: SkillManager,
        browser_worker: HeadlessBrowserWorker,
        desktop_operator: DesktopOperator,
    ) -> None:
        self.config = config
        self.llm_server = llm_server
        self.skill_manager = skill_manager
        self.browser_worker = browser_worker
        self.desktop_operator = desktop_operator

        self.cancel_event = threading.Event()
        self.event_log: list[str] = []
        self._tool_pool = ThreadPoolExecutor(max_workers=3)
        self.local_client = OpenAI(
            base_url=f"{config.llm_base_url}/v1",
            api_key="local-studio",
            timeout=600.0,
            max_retries=0,
        )
        self.toolkit = AgentToolkit(
            config=config,
            skill_manager=skill_manager,
            browser_worker=browser_worker,
            desktop_operator=desktop_operator,
            vision_fn=self.vision_describe,
            log_event_fn=self.log_event,
        )

    def log_event(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.event_log.append(f"[{ts}] {msg}")
        if len(self.event_log) > 200:
            del self.event_log[:50]

    def _get_gemini_client(self) -> OpenAI:
        key = (self.config.gemini_api_key or "").strip()
        if not key:
            raise RuntimeError("Gemini API key is not set. Add your Gemini API key in the System tab or .env file.")
        return OpenAI(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key=key,
            timeout=180.0,
            max_retries=1,
        )

    def vision_describe(self, path: str, question: str = "", max_tokens: int = 512) -> str:
        use_gemini = self.config.provider == "gemini" and bool((self.config.gemini_api_key or "").strip())
        if not use_gemini and not self.llm_server.vision_active:
            return "(Vision is not active - running in text-only mode.)"
        path = path or str(self.config.latest_screen_path)
        if not os.path.isabs(path):
            path = str(self.config.workspace_dir / path)
        if not os.path.exists(path):
            return f"(Image not found: {path})"

        is_screen = os.path.realpath(path) == os.path.realpath(str(self.config.latest_screen_path))
        q = (
            question
            or (
                "Describe this screen concisely. Use the cyan coordinate grid labels (x200.., y200..) "
                "to report exact (x, y) pixel centers of visible buttons, address bars, inputs, and windows."
                if is_screen
                else "Describe this image concisely. Note visible text and key elements."
            )
        ).strip()

        try:
            data_url = encode_image_data_url(path, draw_grid=is_screen)
            vis_msgs = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": q + "\nAnswer directly and briefly."},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ]
            if use_gemini:
                g_client = self._get_gemini_client()
                resp = g_client.chat.completions.create(
                    model=self.config.gemini_model or "gemini-2.5-flash",
                    temperature=0.2,
                    max_tokens=max_tokens,
                    stream=False,
                    messages=vis_msgs,
                )
            else:
                with self.llm_server.llm_lock:
                    resp = self.local_client.chat.completions.create(
                        model=self.config.model_alias,
                        temperature=0.2,
                        max_tokens=max_tokens,
                        stream=False,
                        messages=vis_msgs,
                    )
                    time.sleep(0.15)
            raw = _client_text(resp.choices[0].message)
            ans = strip_think(raw).strip()
            if not ans:
                ans = "(partial) " + re.sub(r"\s+", " ", raw.replace("<think>", ""))[-600:]
            return ans
        except Exception as e:
            self.log_event(f"Vision error: {type(e).__name__}: {e}")
            return f"[Vision Note] Could not inspect image ({type(e).__name__}); continuing with file metadata."

    def fit_history(self, messages: list[dict], budget: int) -> list[dict]:
        for m in messages:
            if (
                m.get("role") == "assistant"
                and isinstance(m.get("content"), str)
                and ("<think>" in m["content"] or "</think>" in m["content"])
            ):
                m["content"] = strip_think(m["content"]).strip() or "(continued)"

        protect = max(1, len(messages) - 6)
        for i in range(1, protect):
            txt = _msg_text(messages[i])
            if messages[i].get("role") == "assistant":
                messages[i]["content"] = _shrink_params(txt, keep=100)
            elif txt.lstrip().startswith("<tool_response") and len(txt) > 420:
                messages[i]["content"] = _head(txt, 360) + "\n</tool_response>"

        def total() -> int:
            return sum(est_tokens(_msg_text(m)) + 6 for m in messages)

        if total() <= budget:
            return messages
        for i in range(1, protect):
            txt = _msg_text(messages[i])
            if txt.lstrip().startswith("<tool_response") and len(txt) > 220:
                messages[i]["content"] = _head(txt, 180) + "\n</tool_response>"
        if total() > budget:
            for i in range(1, protect):
                messages[i]["content"] = _head(_msg_text(messages[i]), 600)
        if total() > budget:
            marker = {
                "role": "user",
                "content": f"[Earlier conversation was trimmed to save context. Files created earlier are still in {self.config.workspace_dir}.]",
            }
            if len(messages) > 1 and messages[1].get("content") == marker["content"]:
                del messages[1]
            dropped = False
            while total() > budget and len(messages) > 5:
                del messages[1]
                dropped = True
            if dropped:
                messages.insert(1, marker)
        if total() > budget:
            for m in messages[1:]:
                if m.get("role") == "assistant":
                    m["content"] = _shrink_params(_msg_text(m), keep=400)
            for m in messages[1:]:
                t = _msg_text(m)
                if total() > budget and len(t) > 4500:
                    m["content"] = truncate_output(t, 4500)
        return messages

    def _stream_raw(self, prompt: str, max_tokens: int, temp: float, abort: threading.Event):
        q: queue.Queue = queue.Queue()

        def worker():
            stream = None
            with self.llm_server.llm_lock:
                try:
                    stream = self.local_client.completions.create(
                        model=self.config.model_alias,
                        prompt=prompt,
                        temperature=temp,
                        top_p=0.95,
                        max_tokens=max_tokens,
                        stop=STOP_SEQS,
                        stream=True,
                        extra_body={"top_k": 20, "min_p": 0.0},
                    )
                    for chunk in stream:
                        if abort.is_set():
                            break
                        if not chunk.choices:
                            continue
                        c = chunk.choices[0]
                        if c.text:
                            q.put(("tok", c.text))
                        if c.finish_reason:
                            q.put(("fin", c.finish_reason))
                except Exception as e:
                    q.put(("err", e))
                finally:
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                    time.sleep(0.18)
                    q.put(("end", None))

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        t0 = time.time()
        try:
            while True:
                try:
                    ev = q.get(timeout=2.0)
                except queue.Empty:
                    yield ("hb", int(time.time() - t0))
                    continue
                if ev[0] == "end":
                    return
                yield ev
        finally:
            abort.set()
            th.join(timeout=10)

    def _stream_gemini(self, history: list[dict], temp: float, max_tokens: int, abort: threading.Event):
        g_client = self._get_gemini_client()
        g_model = self.config.gemini_model or "gemini-2.5-flash"
        q: queue.Queue = queue.Queue()
        clean_msgs = [{"role": m.get("role", "user"), "content": _msg_text(m)} for m in history]

        def worker():
            try:
                stream = g_client.chat.completions.create(
                    model=g_model,
                    messages=clean_msgs,
                    temperature=temp,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for chunk in stream:
                    if abort.is_set():
                        break
                    if not chunk.choices:
                        continue
                    c = chunk.choices[0]
                    delta = getattr(c, "delta", None)
                    txt = getattr(delta, "content", None) or ""
                    if txt:
                        q.put(("tok", txt))
                    if c.finish_reason:
                        q.put(("fin", c.finish_reason))
            except Exception as e:
                q.put(("err", e))
            finally:
                q.put(("end", None))

        th = threading.Thread(target=worker, daemon=True)
        th.start()
        t0 = time.time()
        try:
            while True:
                try:
                    ev = q.get(timeout=2.0)
                except queue.Empty:
                    yield ("hb", int(time.time() - t0))
                    continue
                if ev[0] == "end":
                    return
                yield ev
        finally:
            abort.set()
            th.join(timeout=8)

    def _restart_server_gen(self):
        fut = self._tool_pool.submit(self.llm_server.start, True, False, False)
        t0 = time.time()
        while True:
            done, _ = futures_wait([fut], timeout=2.0)
            if done:
                try:
                    fut.result()
                    yield ("status", "llama.cpp server is back online.")
                except Exception as e:
                    yield ("status", f"Server restart failed: {str(e)[:200]}")
                return
            yield ("hb", f"Restarting llama.cpp server ({int(time.time() - t0)}s)")

    def _run_with_heartbeat(self, fn, label: str, *args):
        fut = self._tool_pool.submit(fn, *args)
        t0 = time.time()
        while True:
            done, _ = futures_wait([fut], timeout=2.0)
            if done:
                try:
                    yield ("result", fut.result())
                except Exception as e:
                    yield ("result", f"[Tool Error] {label} failed: {type(e).__name__}: {e}")
                return
            if self.cancel_event.is_set():
                yield ("result", "[Cancelled by user before the tool finished]")
                return
            yield ("hb", f"{label} running ({int(time.time() - t0)}s)")

    def generate_assistant_message(self, history: list[dict], think_mode: str, temp: float, seed: str = "", allow_budget: bool = True):
        if self.config.provider == "gemini":
            abort = threading.Event()
            full_txt, finish, err = "", None, None
            gen = self._stream_gemini(history, temp, self.config.max_new_tokens_per_call, abort)
            try:
                for ev in gen:
                    if ev[0] == "hb":
                        yield ("hb", f"Gemini ({self.config.gemini_model}) thinking ({ev[1]}s)...")
                    elif ev[0] == "tok":
                        full_txt += ev[1]
                        yield ("stream", full_txt)
                        if self.cancel_event.is_set():
                            abort.set()
                            finish = "cancelled"
                            break
                    elif ev[0] == "fin":
                        finish = ev[1]
                    elif ev[0] == "err":
                        err = ev[1]
            finally:
                gen.close()
            if err is not None:
                raise RuntimeError(f"Gemini API error: {type(err).__name__}: {err}")
            yield ("result", full_txt, finish or "stop")
            return

        if not self.llm_server.is_up(timeout=1.5):
            raise RuntimeError(
                "Local llama.cpp server is offline. Click 'DOWNLOAD & START LOCAL' in the System tab, "
                "or switch the topbar provider to GEMINI."
            )

        ctx = int(self.config.context_size)
        budget_chars = THINK_BUDGETS.get(think_mode, 3500)
        prefill = "" if seed else ("<think>\n\n</think>\n\n" if think_mode == "off" else "")
        base = render_chatml(history) + "<|im_start|>assistant\n"
        generated = seed
        finish, forced, loop_cuts, conn_errors, rounds = None, 0, 0, 0, 0

        while rounds < 14:
            rounds += 1
            if self.cancel_event.is_set():
                finish = "cancelled"
                break
            if est_tokens(generated) > self.config.max_new_tokens_per_call * 3:
                finish = "length_cap"
                break
            prompt = base + prefill + generated
            room = ctx - est_tokens(prompt) - 128
            max_tokens = min(self.config.max_new_tokens_per_call, room)
            if max_tokens < 48:
                finish = "context_full"
                break

            abort = threading.Event()
            why, round_text, round_finish, err = None, "", None, None
            gen = self._stream_raw(prompt, max_tokens, temp, abort)
            try:
                for ev in gen:
                    kind = ev[0]
                    if kind == "hb":
                        yield ("hb", "Generating tokens..." if round_text else f"Processing prompt (~{est_tokens(prompt):,} tokens, {ev[1]}s)")
                    elif kind == "tok":
                        round_text += ev[1]
                        cur = prefill + generated + round_text
                        yield ("stream", cur)
                        if self.cancel_event.is_set():
                            why = "cancel"
                        elif allow_budget and budget_chars and _in_think(cur) and _think_len(cur) > budget_chars:
                            why = "budget"
                        elif _is_looping(cur):
                            why = "loop"
                        if why:
                            abort.set()
                            break
                    elif kind == "fin":
                        round_finish = ev[1]
                    elif kind == "err":
                        err = ev[1]
            finally:
                gen.close()
            generated += round_text

            if err is not None:
                msg = str(err).lower()
                self.log_event(f"LLM stream error: {type(err).__name__}: {err}")
                if "context" in msg and ("exceed" in msg or "window" in msg or "too long" in msg):
                    finish = "context_full"
                    break
                conn_errors += 1
                if conn_errors > 3:
                    raise RuntimeError(
                        f"LLM server failed repeatedly: {type(err).__name__}: {str(err)[:300]}\n"
                        f"--- server log tail ---\n{self.llm_server.log_tail()}"
                    )
                yield ("status", f"Server connection interrupted ({type(err).__name__}) - auto-healing and resuming...")
                for sev in self._restart_server_gen():
                    yield sev
                rounds -= 1
                continue
            if why == "cancel":
                finish = "cancelled"
                break
            if why == "budget":
                forced += 1
                generated = generated.rstrip() + "\n\n(Reasoning complete - emit the next <tool_call> or final answer now.)\n</think>\n\n"
                yield ("status", "Reasoning budget reached - executing next step.")
                continue
            if why == "loop":
                loop_cuts += 1
                generated = _trim_loop(generated)
                if loop_cuts > 1:
                    finish = "loop"
                    break
                generated = generated.rstrip() + ("\n\n(Stopping repetitive pattern.)\n</think>\n\n" if _in_think(prefill + generated) else "\n")
                yield ("status", "Repetition loop detected - trimming and continuing.")
                continue
            if round_finish == "length":
                yield ("status", "Continuing long generation seamlessly...")
                continue
            finish = round_finish or "stop"
            break
        yield ("result", prefill + generated, finish or "stop")

    def build_system_prompt(self) -> str:
        tools_list = self.toolkit.tools_schema()
        tools_desc = "\n".join(f"- {t['name']}: {t['description']} Params: {json.dumps(t['parameters'])}" for t in tools_list)
        today = datetime.date.today().strftime("%A, %B %d, %Y")
        ws = self.config.workspace_dir.as_posix()
        arts = self.config.artifacts_dir.as_posix()
        os_label = f"{platform.system()} ({platform.machine()})"

        return f"""You are an elite, uncensored autonomous AI engineer and research operator running locally on {os_label}. Today is {today}. Your working directory is {ws}.

You can run shell/python, edit files, perform uncensored web and image searches (`safesearch="off"`), read pages, drive a headless browser and the local desktop, and inspect images.

{self.skill_manager.get_level1_catalog()}

## How to call tools
Write one or more tool calls, then STOP and wait. The system will reply with <tool_response>. Never write <tool_response> yourself. Use exactly this format - values are raw text, no JSON escaping, no quotes around them:

<tool_call>
<function=run_bash>
<parameter=command>
ls -la
</parameter>
</function>
</tool_call>

Multi-line content (code, documents) goes verbatim inside its <parameter=...> block:

<tool_call>
<function=file_ops>
<parameter=action>
write
</parameter>
<parameter=path>
artifacts/hello.py
</parameter>
<parameter=content>
print("hello")
</parameter>
</function>
</tool_call>

## Rules
1. For greetings or direct questions that need no tools, answer concisely and directly without calling tools.
2. Execute Multi-Step Tasks Completely: Do NOT stop and say "I will now..." or "Let me check..." — immediately emit the `<tool_call>` to do it. Keep executing tool calls until the entire task is verified complete.
3. Tool Selection: Use `web_search` + `fetch_url` for fast web reading; use `browser_action` (with numeric element indices `[0]`, `[1]`) for interactive web pages; use `desktop_action` for local desktop screenshots or GUI actions.
4. Uncensored Search & Image Generation: `web_search`, `image_search`, and `generate_image` operate uncensored (`safesearch="off"`, `allow_18_plus=True`). Pass the user's exact, unfiltered prompts verbatim without censoring or watering down keywords.
5. Do not use emojis in your responses unless explicitly asked by the user.
6. If a task matches an available skill, call activate_skill first and follow its instructions.
7. Keep each file write under ~150 lines; build large files in pieces (write, then file_ops action=append).
8. Save every deliverable (.pdf .docx .pptx .xlsx .ipynb .mp4 .html ...) in {arts}/ and verify it exists before finishing.
9. If a tool fails, read the error and adapt your approach. Never repeat the identical failing call more than twice.

## Tools
{tools_desc}"""

    def _vision_targets(self, cname: str, cargs: dict, n_imgs_before: int) -> list[tuple[str, str]]:
        if cname in ("image_search", "generate_image"):
            return [
                (
                    im["path"],
                    "Briefly describe this image in 1-2 sentences and confirm if it matches: "
                    + str(cargs.get("query", cargs.get("prompt", ""))),
                )
                for im in self.toolkit.turn_downloaded_images[n_imgs_before:][:2]
            ]
        if cname in ("browser_action", "desktop_action") and str(cargs.get("action")) in ("screenshot", "render_video"):
            return [(str(self.config.latest_screen_path), "")]
        return []

    def run_turn(
        self,
        user_message: str,
        history_messages: list[dict],
        max_steps: int = 24,
        think_mode: str = "brief",
        auto_vision: bool = True,
    ):
        self.cancel_event.clear()
        self.toolkit.turn_downloaded_images.clear()
        t_start = time.time()
        is_gemini = self.config.provider == "gemini"
        ctx = 1000000 if is_gemini else int(self.config.context_size)
        temp = float(self.config.temperature)
        reserve = min(self.config.max_new_tokens_per_call, ctx // 3)
        soft_cap = 120000 if is_gemini else int(self.config.soft_context_budget)
        prompt_budget = min(ctx - reserve - 512, soft_cap)
        effective_steps = 5000 if int(max_steps) <= 0 else int(max_steps)

        sys_msg = {"role": "system", "content": self.build_system_prompt()}
        if history_messages and history_messages[0].get("role") == "system":
            history_messages[0] = sys_msg
        else:
            history_messages.insert(0, sys_msg)
        history_messages.append({"role": "user", "content": user_message})

        seen: Counter = Counter()
        malformed, last_text, intent_nudges = 0, "", 0

        def stats_line() -> str:
            used = est_tokens(render_chatml(history_messages))
            return f"{time.time() - t_start:.1f}s  |  context: {used:,} / {ctx:,} tokens  |  reasoning: {think_mode}"

        for step in range(effective_steps):
            if self.cancel_event.is_set():
                yield ("done", "[Stopped]")
                return
            self.fit_history(history_messages, prompt_budget)

            text, finish = "", "stop"
            for ev in self.generate_assistant_message(history_messages, think_mode, temp):
                if ev[0] == "stream":
                    yield ("assistant_stream", render_display(ev[1]))
                elif ev[0] == "hb":
                    yield ("heartbeat", ev[1])
                elif ev[0] == "status":
                    yield ("status", ev[1])
                elif ev[0] == "result":
                    text, finish = ev[1], ev[2]
            last_text = text

            if finish == "cancelled":
                yield ("done", render_display(text, final=True) or "[Stopped]")
                return

            visible = strip_think(text).strip()
            calls = parse_tool_calls(visible)

            if finish == "context_full" and not calls:
                self.fit_history(history_messages, int(prompt_budget * 0.6))
                yield ("status", "Context limit reached - compacted history and retrying.")
                history_messages.append({"role": "user", "content": "Your previous reply was cut off because the context was full. Continue the task concisely."})
                continue

            if not calls:
                has_markup = "<tool_call>" in visible or "<function=" in visible
                if has_markup and malformed < 3:
                    malformed += 1
                    history_messages.append({"role": "assistant", "content": _strip_markup(visible) or "(tool call)"})
                    history_messages.append({
                        "role": "user",
                        "content": (
                            '<tool_response name="system">\nYour tool call could not be parsed. Re-send it using EXACTLY this format '
                            "(raw values, no JSON):\n<tool_call>\n<function=TOOL_NAME>\n<parameter=PARAM>\nVALUE\n</parameter>\n</function>\n</tool_call>\n"
                            "If the content is long, split it into smaller writes.\n</tool_response>"
                        ),
                    })
                    yield ("status", "Malformed tool call - requesting valid XML format.")
                    continue

                answer = _strip_markup(visible)
                if answer and len(answer) < 260 and intent_nudges < 1 and step < effective_steps - 1:
                    low_a = answer.lower().strip()
                    if (
                        low_a.endswith(":")
                        or low_a.endswith("...")
                        or any(low_a.startswith(p) for p in ("let me ", "i will ", "i'll ", "now i will ", "next, i will ", "taking a screenshot", "opening "))
                    ):
                        intent_nudges += 1
                        history_messages.append({"role": "assistant", "content": answer})
                        history_messages.append({
                            "role": "user",
                            "content": '<tool_response name="system">\nDo not narrate what you will do — emit the actual <tool_call> now to execute it.\n</tool_response>',
                        })
                        yield ("status", "Prompting agent to execute tool call...")
                        continue

                if not answer:
                    seed = text if text.strip() else ""
                    if _in_think(seed):
                        seed = seed.rstrip() + "\n</think>\n\n"
                    elif seed.strip():
                        seed = seed.rstrip() + "\n\n"
                    yield ("status", "Generating final response...")
                    retry_text = ""
                    for ev in self.generate_assistant_message(history_messages, think_mode, temp, seed=seed, allow_budget=False):
                        if ev[0] == "stream":
                            yield ("assistant_stream", render_display(ev[1]))
                        elif ev[0] == "hb":
                            yield ("heartbeat", ev[1])
                        elif ev[0] == "result":
                            retry_text = ev[1]
                    answer = _strip_markup(strip_think(retry_text).strip())
                    text = retry_text or text

                if not answer:
                    gist = re.sub(r"\s+", " ", text.replace("<think>", "").replace("</think>", ""))[-600:]
                    answer = (
                        "*The model completed reasoning without emitting a final response. Try again or set Reasoning to `off`.*"
                        + (f"\n\n> Last thoughts: {gist}" if gist.strip() else "")
                    )
                if finish in ("length_cap", "context_full", "loop"):
                    answer += "\n\n*(Output truncated at limit - send `continue` to resume.)*"
                history_messages.append({"role": "assistant", "content": answer})
                yield ("stats", stats_line())
                reasoning_view = render_display(text, final=True)
                yield ("done", reasoning_view if reasoning_view.strip() else answer)
                return

            history_messages.append({"role": "assistant", "content": visible})
            responses = []
            for cname, cargs in calls:
                if self.cancel_event.is_set():
                    break
                key = (cname, json.dumps(cargs, sort_keys=True, default=str))
                seen[key] += 1
                yield ("tool_start", f"{cname}({json.dumps(cargs, default=str)[:200]})")
                n_before = len(self.toolkit.turn_downloaded_images)
                result = ""
                for ev in self._run_with_heartbeat(self.toolkit.dispatch, cname, cname, cargs):
                    if ev[0] == "hb":
                        yield ("heartbeat", ev[1])
                    else:
                        result = ev[1]
                result = truncate_output(str(result), 6000)

                vision_ready = (is_gemini and bool(self.config.gemini_api_key)) or self.llm_server.vision_active
                if auto_vision and vision_ready:
                    for img_path, q in self._vision_targets(cname, cargs, n_before):
                        for ev in self._run_with_heartbeat(self.vision_describe, "Inspecting image", img_path, q):
                            if ev[0] == "hb":
                                yield ("heartbeat", ev[1])
                            else:
                                result += f"\n\n[Vision notes for {img_path}]\n{ev[1]}"
                if seen[key] >= 3:
                    result += "\n\n[system] You have already run this exact call 3 times. Change your approach or give your final answer."
                yield ("tool_end", f"{cname} -> {result[:460]}")
                responses.append(f'<tool_response name="{cname}">\n{result}\n</tool_response>')

            if self.cancel_event.is_set():
                yield ("done", "[Stopped]")
                return
            history_messages.append({"role": "user", "content": "\n".join(responses)})

        yield ("stats", stats_line())
        yield ("done", (render_display(last_text, final=True) or "") + "\n\n*(Reached max autonomous steps for this turn - send `continue` to resume.)*")
