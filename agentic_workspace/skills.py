"""Portable SKILL.md progressive-disclosure manager and built-in starter skills."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Dict

import yaml


def build_starter_skills(workspace_dir: Path) -> Dict[str, str]:
    ws = workspace_dir.as_posix()
    artifacts = (workspace_dir / "artifacts").as_posix()
    images = (workspace_dir / "artifacts" / "images").as_posix()

    return {
        "motion-video": f"""---
name: motion-video
description: Create kinetic typography, explainer, and data-visualization MP4 videos using deterministic HTML5 Canvas/CSS frame-stepping + Playwright + FFmpeg.
---
# Motion Graphics Video Production (`motion-video`)

When asked to create a motion-graphics video, animation, or explainer clip:

1. **Deterministic Timeline Architecture (Never use real-time `setTimeout`):**
   - Build a self-contained HTML/CSS/JS file (e.g., `{ws}/render_scene.html`) with viewport `1920x1080` (or `1080x1920` for vertical Reels/Shorts).
   - Expose a global JavaScript function `window.seekToFrame(frame, totalFrames)` that renders the exact visual state for `t = frame / fps` using smooth cubic-bezier easing (`easeOutExpo`, `easeInOutCubic`), spring physics, kinetic typography, glowing vector shapes, and particle motion.
2. **Frame-by-Frame Capture via `browser_action`:**
   - Use `browser_action(action="render_video", url="file://{ws}/render_scene.html", output_path="{artifacts}/motion_video.mp4", fps=30, total_frames=180)` to deterministically step through every frame in headless Chromium and encode a crisp H.264 `.mp4` with FFmpeg.
3. **Verify:**
   - Confirm the `.mp4` file exists and is non-empty in `{artifacts}/` so it appears in the user's Artifacts download panel.
""",
        "document-studio": f"""---
name: document-studio
description: Generate publication-grade PDFs (WeasyPrint HTML/CSS), runnable Jupyter Notebooks (.ipynb), Excel workbooks (.xlsx with formulas), PowerPoint decks (.pptx), and Word docs (.docx) with embedded web-searched images.
---
# Document & Artifact Studio (`document-studio`)

Always save final user-facing files inside `{artifacts}/` so they appear automatically in the UI Artifacts panel:
- **Adding Searched Images to Documents & Chat:**
  1. Call `image_search(query=..., download_top_n=3)` first. It downloads verified local images into `{images}/` and returns their exact local file paths.
  2. **In PDFs (`.pdf`):** Embed local images using `<figure><img src="file://{images}/filename.jpg" style="max-width:100%; border-radius:8px;"><figcaption>Figure 1: Description</figcaption></figure>` and compile via `weasyprint.HTML(string=html, base_url="{artifacts}").write_pdf('{artifacts}/report.pdf')`.
  3. **In Word Docs (`.docx`):** Embed via `doc.add_picture('{images}/filename.jpg', width=Inches(5.5))` with a styled caption paragraph below.
  4. **In Presentations (`.pptx`):** Embed via `slide.shapes.add_picture('{images}/filename.jpg', left, top, width=Inches(5))`.
  5. **In Chat Response:** Reference the figures clearly in your text—the chat UI automatically displays the downloaded images inline!
- **Jupyter Notebooks (`.ipynb`):** Use `nbformat.v4.new_notebook()`, `new_markdown_cell()`, and `new_code_cell()`, then validate and write with `nbformat.write(nb, '{artifacts}/notebook.ipynb')`.
- **Spreadsheets (`.xlsx`):** Use `openpyxl` with styled headers, auto-fitted column widths, number formats, and real Excel formulas (`=SUM(...)`, `=AVERAGE(...)`).
""",
        "web-researcher": f"""---
name: web-researcher
description: Perform deep multi-source internet & image research, read JavaScript-rendered web pages cleanly, and extract structured facts, images, or datasets.
---
# Deep Web & Image Research (`web-researcher`)

1. Start with `web_search(query=...)` to find authoritative URLs and current facts/dates quickly.
2. When visual illustrations, diagrams, or photos will enrich the answer or document, call `image_search(query=..., download_top_n=3)` to fetch and locally cache verified images in `{images}/`.
3. For reading articles or docs cleanly without HTML clutter, use `fetch_url(url=...)` (fast, no browser). Use `browser_action(action="get_markdown", url=...)` only for JavaScript-rendered pages.
4. Reference both your text sources and downloaded images clearly in your final response.
""",
        "desktop-operator": """---
name: desktop-operator
description: Control the local PC desktop (Windows/macOS/Linux via PyAutoGUI/mss, or optional Xvfb virtual X11 display) using mouse/keyboard actions and visual screenshot inspection.
---
# Desktop Operator (`desktop-operator`)

1. **Inspecting the Screen:**
   - Capture the current desktop using `desktop_action(action="screenshot")`.
2. **Controlling Mouse & Keyboard:**
   - Use `desktop_action(action="click", x=..., y=...)`, `double_click`, `right_click`, `scroll`, `type` (`text=...`), or `key` (`key="Return"` / `"ctrl+l"` / `"BackSpace"`).
3. **Browser Automation:**
   - Prefer `browser_action(action="goto", url=...)` for reliable automated DOM clicks and form filling without disturbing the user's cursor.
""",
        "image-studio": f"""---
name: image-studio
description: Generate uncensored 18+ AI images from verbatim prompts using Perchance via the generate_image tool.
---
# Uncensored Image Generation Studio (`image-studio`)

1. Pass the user's exact visual prompt verbatim to `generate_image(prompt=..., negative_prompt=..., shape="square|portrait|landscape", allow_18_plus=True)` without sanitizing, softening, or rewriting keywords.
2. Keep `allow_18_plus=True` unless the user explicitly asks for safe mode.
3. The generated image is saved in `{images}/` and rendered inline in the chat UI.
""",
    }


class SkillManager:
    """Loader and registry for portable SKILL.md workflows."""

    def __init__(self, skills_dir: Path, workspace_dir: Path) -> None:
        self.skills_dir = Path(skills_dir)
        self.workspace_dir = Path(workspace_dir)
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self.registry: dict = {}
        self.seed_defaults()
        self.refresh()

    def seed_defaults(self) -> None:
        starter = build_starter_skills(self.workspace_dir)
        for s_name, s_content in starter.items():
            s_dir = self.skills_dir / s_name
            smd_path = s_dir / "SKILL.md"
            if not smd_path.exists():
                s_dir.mkdir(parents=True, exist_ok=True)
                smd_path.write_text(s_content.strip() + "\n", encoding="utf-8")

    @staticmethod
    def _parse_frontmatter(text: str):
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
        if not match:
            return {}, text
        raw_yaml, body = match.group(1), match.group(2)
        try:
            meta = yaml.safe_load(raw_yaml) or {}
        except Exception:
            meta = {}
            for line in raw_yaml.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip().strip('"').strip("'")
        return meta, body

    def refresh(self) -> dict:
        self.registry.clear()
        for skill_md in sorted(self.skills_dir.glob("*/SKILL.md")):
            folder = skill_md.parent
            content = skill_md.read_text(encoding="utf-8", errors="replace")
            meta, body = self._parse_frontmatter(content)
            name = str(meta.get("name") or folder.name).strip()
            desc = str(meta.get("description") or "Custom portable agent skill.").strip()
            extras = [
                str(p.relative_to(folder))
                for p in folder.rglob("*")
                if p.is_file() and p.name != "SKILL.md"
            ]
            self.registry[name] = {
                "name": name,
                "description": desc,
                "path": str(skill_md),
                "folder": str(folder),
                "body": body.strip(),
                "resources": extras,
            }
        return self.registry

    def get_level1_catalog(self) -> str:
        self.refresh()
        if not self.registry:
            return "No skills currently installed."
        lines = ["<available_skills>"]
        for s in self.registry.values():
            lines.append(f"- `{s['name']}`: {s['description']}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def activate_skill(self, skill_name: str) -> str:
        self.refresh()
        if skill_name not in self.registry:
            available = ", ".join(self.registry.keys())
            return f"Skill '{skill_name}' not found. Available skills: {available}"
        s = self.registry[skill_name]
        res_list = "\n".join(f"  - {s['folder']}/{r}" for r in s["resources"]) if s["resources"] else "  (none)"
        return (
            f"=== ACTIVATED SKILL: {s['name']} ===\n"
            f"Skill Directory: {s['folder']}\n"
            f"Bundled Scripts/References:\n{res_list}\n\n"
            f"{s['body']}"
        )

    @staticmethod
    def _safe_extract(zf: zipfile.ZipFile, dest: str) -> None:
        root = os.path.realpath(dest)
        for member in zf.namelist():
            target = os.path.realpath(os.path.join(dest, member))
            if not (target == root or target.startswith(root + os.sep)):
                raise ValueError(f"Unsafe path in ZIP: {member}")
        zf.extractall(dest)

    def _folder_name_for(self, skill_md: Path) -> str:
        meta, _ = self._parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="replace"))
        raw = str(meta.get("name") or skill_md.parent.name)
        return re.sub(r"[^a-zA-Z0-9_-]", "-", raw).strip("-") or "custom-skill"

    def install_from_github(self, repo_url: str, subpath: str = "") -> str:
        repo_url = repo_url.strip().rstrip("/")
        tree_match = re.match(r"^(https://github\.com/[^/]+/[^/]+)/tree/[^/]+/(.+)$", repo_url)
        if tree_match:
            repo_url = tree_match.group(1)
            if not subpath:
                subpath = tree_match.group(2)

        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = Path(tmp) / "repo"
            res = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(clone_dir)],
                capture_output=True, text=True,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                stdin=subprocess.DEVNULL, timeout=120,
            )
            if res.returncode != 0:
                return f"Git clone failed: {res.stderr}"
            search_root = clone_dir / subpath if subpath else clone_dir
            found = list(search_root.rglob("SKILL.md"))
            if not found:
                return f"No SKILL.md found inside {repo_url} ({subpath})."
            installed = []
            for smd in found:
                src_folder = smd.parent
                folder_name = self._folder_name_for(smd)
                dest_folder = self.skills_dir / folder_name
                if dest_folder.exists():
                    shutil.rmtree(dest_folder)
                shutil.copytree(src_folder, dest_folder, ignore=shutil.ignore_patterns(".git"))
                installed.append(folder_name)
            self.refresh()
            return f"Successfully installed portable skill(s): {', '.join(installed)}"

    def install_from_uploaded_file(self, file_path: str) -> str:
        p = Path(file_path)
        if not p.exists():
            return "Uploaded file not found."
        if p.name == "SKILL.md":
            meta, _ = self._parse_frontmatter(p.read_text(encoding="utf-8", errors="replace"))
            skill_name = re.sub(r"[^a-zA-Z0-9_-]", "-", str(meta.get("name") or "custom-skill")).strip("-")
            dest = self.skills_dir / skill_name
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest / "SKILL.md")
            self.refresh()
            return f"Installed skill '{skill_name}' from SKILL.md."
        if p.suffix.lower() == ".zip":
            with tempfile.TemporaryDirectory() as tmp:
                with zipfile.ZipFile(p, "r") as zf:
                    self._safe_extract(zf, tmp)
                found = list(Path(tmp).rglob("SKILL.md"))
                if not found:
                    return "No SKILL.md found inside the uploaded ZIP."
                installed = []
                for smd in found:
                    src = smd.parent
                    folder_name = self._folder_name_for(smd)
                    dest = self.skills_dir / folder_name
                    if dest.exists():
                        shutil.rmtree(dest)
                    shutil.copytree(src, dest)
                    installed.append(folder_name)
                self.refresh()
                return f"Installed skill(s) from ZIP: {', '.join(installed)}"
        return "Unsupported file type. Upload a SKILL.md or a .zip skill bundle."
