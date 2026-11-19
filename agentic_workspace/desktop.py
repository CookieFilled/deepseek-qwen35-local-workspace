"""Cross-platform desktop automation (Windows/macOS/Linux native via mss/pyautogui, plus optional Linux Xvfb/noVNC)."""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import time
from pathlib import Path

from .config import WorkspaceConfig


def _tcp_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _norm_xdotool_key(k: str) -> str:
    parts = [p.strip() for p in (k or "").split("+") if p.strip()]
    mapped = []
    for p in parts:
        low = p.lower()
        if low in ("ctrl", "control"):
            mapped.append("ctrl")
        elif low in ("alt", "shift", "super"):
            mapped.append(low)
        elif low in ("enter", "return"):
            mapped.append("Return")
        elif low in ("esc", "escape"):
            mapped.append("Escape")
        elif low == "tab":
            mapped.append("Tab")
        elif low == "backspace":
            mapped.append("BackSpace")
        else:
            mapped.append(p)
    return "+".join(mapped)


def _norm_pyautogui_keys(k: str) -> list[str]:
    key_map = {
        "control": "ctrl",
        "return": "enter",
        "escape": "esc",
        "super": "win" if platform.system() == "Windows" else "command",
        "backspace": "backspace",
    }
    return [key_map.get(p.strip().lower(), p.strip().lower()) for p in (k or "").split("+") if p.strip()]


class DesktopOperator:
    """Handles screenshots and GUI control on both native local desktops and optional Linux Xvfb displays."""

    def __init__(self, config: WorkspaceConfig) -> None:
        self.config = config
        self.use_xvfb = (
            platform.system() == "Linux"
            and config.enable_virtual_desktop
            and bool(shutil.which("Xvfb"))
        )
        if self.use_xvfb:
            self.ensure_vnc_running()

    def ensure_vnc_running(self) -> bool:
        if platform.system() != "Linux" or not self.config.enable_virtual_desktop:
            return False
        if _tcp_open("127.0.0.1", 5900, timeout=0.4):
            return True
        if not shutil.which("x11vnc") or not shutil.which("Xvfb"):
            return False
        try:
            if subprocess.run(["pgrep", "-f", "Xvfb :99"], capture_output=True).returncode != 0:
                subprocess.Popen(
                    ["Xvfb", ":99", "-screen", "0", "1920x1080x24", "-ac"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                )
                time.sleep(1.0)
                if shutil.which("fluxbox"):
                    subprocess.Popen(
                        ["fluxbox"], env={**os.environ, "DISPLAY": ":99"},
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                    )
            subprocess.Popen(
                [
                    "x11vnc", "-display", ":99", "-forever", "-shared", "-nopw",
                    "-rfbport", "5900", "-listen", "127.0.0.1", "-noxdamage", "-xkb",
                ],
                env={**os.environ, "DISPLAY": ":99"},
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
            )
            time.sleep(0.8)
            return _tcp_open("127.0.0.1", 5900, timeout=0.8)
        except Exception:
            return False

    def capture_screenshot(self, dest: Path | None = None) -> tuple[bool, str]:
        target = dest or self.config.latest_screen_path
        target.parent.mkdir(parents=True, exist_ok=True)

        if self.use_xvfb and shutil.which("scrot"):
            try:
                subprocess.run(
                    ["scrot", "-o", str(target)],
                    env={**os.environ, "DISPLAY": ":99"},
                    check=True, timeout=20, stdin=subprocess.DEVNULL, capture_output=True,
                )
                return True, f"Captured virtual X11 screenshot -> {target}"
            except Exception as e:
                return False, f"[Desktop Error] scrot failed: {e}"

        # Native cross-platform screenshot via mss (preferred) or PIL.ImageGrab
        try:
            import mss  # type: ignore
            from PIL import Image

            with mss.mss() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                raw = sct.grab(mon)
                im = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                im.save(target, format="PNG")
                return True, f"Captured local desktop screenshot -> {target} ({im.width}x{im.height})"
        except ImportError:
            pass
        except Exception as e:
            return False, f"[Desktop Error] mss capture failed: {e}"

        try:
            from PIL import ImageGrab

            im = ImageGrab.grab()
            im.save(target, format="PNG")
            return True, f"Captured local desktop screenshot -> {target} ({im.width}x{im.height})"
        except Exception as e:
            return False, f"[Desktop Error] Screenshot unavailable ({e}). Install 'mss' or enable Playwright browser screenshots."

    def _x(self, cmd: list[str], timeout: int = 20):
        return subprocess.run(
            cmd, env={**os.environ, "DISPLAY": ":99"}, check=True,
            timeout=timeout, stdin=subprocess.DEVNULL, capture_output=True,
        )

    def _active_window_title_x11(self) -> str:
        try:
            r = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                env={**os.environ, "DISPLAY": ":99"},
                capture_output=True, text=True, timeout=3,
            )
            return r.stdout.strip() or "(none)"
        except Exception:
            return "(unknown)"

    def action(self, action: str, x: int = 0, y: int = 0, text: str = "", key: str = "") -> str:
        if action == "screenshot":
            _, msg = self.capture_screenshot()
            return msg

        if self.use_xvfb and shutil.which("xdotool"):
            try:
                if action == "click":
                    self._x(["xdotool", "mousemove", str(x), str(y), "click", "1"])
                elif action == "double_click":
                    self._x(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2", "1"])
                elif action == "right_click":
                    self._x(["xdotool", "mousemove", str(x), str(y), "click", "3"])
                elif action == "scroll":
                    self._x(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "5", "5" if (text or "down") != "up" else "4"])
                elif action == "type":
                    self._x(["xdotool", "type", "--clearmodifiers", "--delay", "20", text], timeout=60)
                elif action == "key":
                    self._x(["xdotool", "key", "--clearmodifiers", _norm_xdotool_key(key)])
                else:
                    return f"Unknown desktop action: {action}. Use screenshot|click|double_click|right_click|scroll|type|key."
                time.sleep(0.4)
                self.capture_screenshot()
                return f"Executed X11 '{action}'. Active Window: {self._active_window_title_x11()}"
            except Exception as e:
                return f"[Desktop Error] {type(e).__name__}: {e}"

        # Native Windows/macOS/Linux GUI control via PyAutoGUI
        try:
            import pyautogui  # type: ignore

            pyautogui.FAILSAFE = True
            if action == "click":
                pyautogui.click(x, y)
            elif action == "double_click":
                pyautogui.doubleClick(x, y)
            elif action == "right_click":
                pyautogui.rightClick(x, y)
            elif action == "scroll":
                pyautogui.moveTo(x, y)
                pyautogui.scroll(500 if (text or "down").lower() == "up" else -500)
            elif action == "type":
                pyautogui.write(text, interval=0.02)
            elif action == "key":
                keys = _norm_pyautogui_keys(key)
                if len(keys) > 1:
                    pyautogui.hotkey(*keys)
                elif keys:
                    pyautogui.press(keys[0])
            else:
                return f"Unknown desktop action: {action}. Use screenshot|click|double_click|right_click|scroll|type|key."
            time.sleep(0.4)
            self.capture_screenshot()
            return f"Executed desktop '{action}' at ({x}, {y})."
        except ImportError:
            return "[Desktop Error] PyAutoGUI is not installed. Run: pip install pyautogui mss"
        except Exception as e:
            return f"[Desktop Error] {type(e).__name__}: {e}"
