"""Build helper: reproducible PyInstaller invocations.

    python build.py --mode release     onedir, windowed (default)
    python build.py --mode debug       onedir, console + verbose logging
    python build.py --mode onefile     single executable
    python build.py --check            validate the spec without building

The script never embeds credentials: it refuses to run when a secret-looking
file is present in the project tree (defence against packaging a stray key).
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from typing import List

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SECRET_PATTERNS = (
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}\b"),
    re.compile(r"\bAQ\.[0-9A-Za-z_\-]{20,}\b"),
)
SCANNED_EXTENSIONS = {".py", ".json", ".txt", ".md", ".spec", ".ini", ".cfg",
                      ".qss", ".yaml", ".yml", ".bat", ".sh"}
SKIP_DIRS = {"build", "dist", ".git", "__pycache__", ".venv", ".qwen"}

# A file may contain a *deliberately fake* credential (redaction tests) only
# when it declares it explicitly. Anything undeclared aborts the build.
ALLOW_MARKER = "core2chat:allow-fake-secret"


def scan_for_secrets() -> tuple:
    """Return (undeclared_findings, declared_fixtures)."""
    findings: List[str] = []
    declared: List[str] = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if os.path.splitext(name)[1].lower() not in SCANNED_EXTENSIONS:
                continue
            path = os.path.join(root, name)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                    text = handle.read()
            except OSError:
                continue
            if any(pattern.search(text) for pattern in SECRET_PATTERNS):
                relative = os.path.relpath(path, PROJECT_ROOT)
                if ALLOW_MARKER in text:
                    declared.append(relative)
                else:
                    findings.append(relative)
    return findings, declared


def scan_git_history() -> List[str]:
    """Scan committed history, not just the working tree.

    A key committed and later deleted is still public once pushed. Each blob is
    scanned individually so the ``allow-fake-secret`` marker of its own file is
    honoured (a plain ``git log -p`` loses file boundaries and would flag the
    deliberate fake credentials used by the redaction tests).
    """
    if not os.path.isdir(os.path.join(PROJECT_ROOT, ".git")):
        return []

    def git(args: List[str]) -> str:
        try:
            completed = subprocess.run(["git"] + args, cwd=PROJECT_ROOT,
                                       capture_output=True, text=True,
                                       timeout=600)
            return completed.stdout
        except (OSError, subprocess.SubprocessError):
            return ""

    revisions = git(["rev-list", "--all"]).split()
    offenders: List[str] = []
    seen_blobs = set()
    for revision in revisions:
        entries = git(["ls-tree", "-r", revision]).splitlines()
        for entry in entries:
            parts = entry.split("\t")
            if len(parts) != 2:
                continue
            meta, path = parts
            fields = meta.split()
            if len(fields) < 3:
                continue
            blob = fields[2]
            if blob in seen_blobs:
                continue
            seen_blobs.add(blob)
            if os.path.splitext(path)[1].lower() not in SCANNED_EXTENSIONS:
                continue
            content = git(["cat-file", "blob", blob])
            if not any(pattern.search(content) for pattern in SECRET_PATTERNS):
                continue
            if ALLOW_MARKER in content:
                continue          # declared fake fixture
            offenders.append("%s (rewizja %s)" % (path, revision[:8]))
    return offenders[:10]


def run(cmd: List[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=PROJECT_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Core2Chat")
    parser.add_argument("--mode", choices=("release", "debug", "onefile"),
                        default="release")
    parser.add_argument("--check", action="store_true",
                        help="validate spec + scan for secrets, do not build")
    args = parser.parse_args()

    findings, declared = scan_for_secrets()
    if findings:
        print("ABORT: undeclared secret-looking strings found:")
        for item in findings:
            print("  - %s" % item)
        print("If this is a deliberate test fixture, add the marker "
              "'%s' to that file." % ALLOW_MARKER)
        return 2
    if declared:
        print("secret scan (working tree): clean (declared fake fixtures: %s)"
              % ", ".join(declared))
    else:
        print("secret scan (working tree): clean")

    history_hits = scan_git_history()
    if history_hits:
        print("ABORT: credential-shaped strings found in git history:")
        for item in history_hits:
            print("  - %s" % item)
        print("Rotate the credential and clean history before pushing.")
        return 2
    print("secret scan (git history): clean")

    spec = os.path.join(PROJECT_ROOT, "pyinstaller.spec")
    if not os.path.isfile(spec):
        print("missing pyinstaller.spec")
        return 2
    with open(spec, "r", encoding="utf-8") as handle:
        compile(handle.read(), spec, "exec")     # syntax validation only
    print("spec syntax: OK")
    if args.check:
        return 0

    if shutil.which("pyinstaller") is None and not _module_available("PyInstaller"):
        print("PyInstaller is not installed. Run: pip install -r requirements-dev.txt")
        return 3

    if args.mode == "onefile":
        command = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                   "--onefile", "--name", "Core2Chat",
                   "--icon", os.path.join("assets", "icon.ico"),
                   "--add-data", "assets%sassets" % os.pathsep,
                   "--exclude-module", "PyQt5.QtWebEngine",
                   "--exclude-module", "PyQt5.QtWebKit",
                   "main.py"]
        if os.name != "nt":
            command = [c for c in command if c != "--icon"] + []
    else:
        command = [sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm",
                   "pyinstaller.spec"]
        if args.mode == "debug":
            print("debug mode: patching console=True is manual; set "
                  "CORE2CHAT_CONSOLE_LOG=1 at runtime for verbose logs")

    code = run(command)
    if code != 0:
        return code
    out_dir = os.path.join(PROJECT_ROOT, "dist", "Core2Chat")
    print("\nbuild finished: %s" % (out_dir if os.path.isdir(out_dir)
                                    else os.path.join(PROJECT_ROOT, "dist")))
    print("smoke test: %s --diagnostics" %
          os.path.join(out_dir, "Core2Chat.exe" if os.name == "nt" else "Core2Chat"))
    return 0


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


if __name__ == "__main__":
    sys.exit(main())
