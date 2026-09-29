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
from typing import Dict, List

# Plugin files dropped together with their libraries (mirrors pyinstaller.spec;
# kept in sync by tests/test_py38_compat.py).
UNWANTED_QT_PLUGIN_FILES = (
    "libqvnc.so",
    "libqwebgl.so",
    "libqsvg.so",
    "libqsvgicon.so",
    "libqpdf.so",
    "libqeglfs.so",
    "libqlinuxfb.so",
    "libqminimalegl.so",
    "libqwayland-egl.so",
    "libqwayland-generic.so",
    "libqwayland-xcomposite-egl.so",
    "libqwayland-xcomposite-glx.so",
    "libqtga.so",
    "libqtiff.so",
    "libqwbmp.so",
    "libqicns.so",
    "qsvg.dll",
    "qsvgicon.dll",
    "qpdf.dll",
    "qwebgl.dll",
    "qvnc.dll",
    "qtga.dll",
    "qtiff.dll",
    "qwbmp.dll",
    "qicns.dll",
)

# Qt libraries the bundle must not carry. This list is generated from
# pyinstaller.spec and kept in sync by
# tests/test_py38_compat.py::test_bundle_trimming_lists_are_consistent, so
# --verify-dist can tell "we removed it" from "the host lacks it".
UNWANTED_QT_LIBS = (
    "libQt5Bluetooth",
    "libQt5Help",
    "libQt5Location",
    "libQt5Multimedia",
    "libQt5Nfc",
    "libQt5OpenGL",
    "libQt5Positioning",
    "libQt5PrintSupport",
    "libQt5Quick",
    "libQt5Qml",
    "libQt5RemoteObjects",
    "libQt5Sensors",
    "libQt5SerialPort",
    "libQt5Svg",
    "libQt5TextToSpeech",
    "libQt5WebChannel",
    "libQt5WebSockets",
    "libQt5Xml",
    "libQt5XmlPatterns",
    "libQt5X11Extras",
    "libQt53D",
    "libQt5Test",
    "libQt5Sql",
    "libQt5Designer",
    "libQt5Network",
    "libQt5Concurrent",
    "libQt5PositioningQuick",
    "libQt5QuickControls2",
    "libQt5QuickTemplates2",
    "libQt5QuickWidgets",
    "libQt5QmlModels",
    "libQt5QmlWorkerScript",
    "libQt5VirtualKeyboard",
    "Qt5Bluetooth",
    "Qt5Help",
    "Qt5Location",
    "Qt5Multimedia",
    "Qt5Nfc",
    "Qt5OpenGL",
    "Qt5Positioning",
    "Qt5PrintSupport",
    "Qt5Quick",
    "Qt5Qml",
    "Qt5RemoteObjects",
    "Qt5Sensors",
    "Qt5SerialPort",
    "Qt5Svg",
    "Qt5TextToSpeech",
    "Qt5WebChannel",
    "Qt5WebSockets",
    "Qt5Xml",
    "Qt5XmlPatterns",
    "Qt5X11Extras",
    "Qt53D",
    "Qt5Test",
    "Qt5Sql",
    "Qt5Designer",
    "Qt5Network",
    "Qt5Concurrent",
)

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


def _system_library_dirs() -> List[str]:
    """Directories the dynamic loader searches outside the bundle."""
    candidates = ["/lib", "/lib64", "/usr/lib", "/usr/lib64",
                  "/usr/lib/x86_64-linux-gnu", "/lib/x86_64-linux-gnu"]
    ld_conf = "/etc/ld.so.conf"
    if os.path.isfile(ld_conf):
        try:
            with open(ld_conf, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line and not line.startswith("#") \
                            and not line.startswith("include"):
                        candidates.append(line)
        except OSError:
            pass
    extra = os.environ.get("LD_LIBRARY_PATH", "")
    candidates.extend(part for part in extra.split(os.pathsep) if part)
    return candidates


def _present_on_system(library: str) -> bool:
    """True when the OS provides the library (so it is not our trimming)."""
    import glob

    for directory in _system_library_dirs():
        if glob.glob(os.path.join(directory, library)) or \
                glob.glob(os.path.join(directory, "**", library),
                          recursive=False):
            return True
    return False


def verify_dist(dist_dir: str) -> tuple:
    """Check the bundle's dynamic dependency closure (POSIX only).

    Trimming unused Qt modules is only safe if nothing that remains links
    against something that was removed. Every ELF binary in the bundle is
    inspected and unresolved references are classified:

    * library is in the bundle or provided by the OS  -> fine;
    * library was dropped by our own trim list        -> **build failure**;
    * library is simply not installed in this container -> warning, because the
      target platform (Windows) does not use it at all.
    """
    if not os.path.isdir(dist_dir):
        return False, ["dist directory missing: %s" % dist_dir]
    if os.name == "nt":
        return True, ["pominięto: weryfikacja domknięcia wymaga narzędzi ELF; "
                      "na Windows użyj dumpbin /dependents ręcznie"]

    import subprocess

    present = set()
    binaries: List[str] = []
    for base, _dirs, files in os.walk(dist_dir):
        for name in files:
            path = os.path.join(base, name)
            present.add(name)
            if ".so" in name:
                binaries.append(path)

    trimmed_prefixes = tuple(
        name.split("/")[-1] for name in UNWANTED_QT_LIBS)
    removed_by_us: Dict[str, set] = {}
    absent_on_host: Dict[str, set] = {}

    for path in binaries:
        try:
            completed = subprocess.run(["ldd", path], capture_output=True,
                                       text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        for line in completed.stdout.splitlines():
            if "not found" not in line:
                continue
            library = line.strip().split()[0]
            if library in present or library.startswith(("linux-vdso",
                                                         "ld-linux")):
                continue
            owner = os.path.basename(path)
            if library.startswith(trimmed_prefixes):
                removed_by_us.setdefault(library, set()).add(owner)
            elif _present_on_system(library):
                continue
            else:
                absent_on_host.setdefault(library, set()).add(owner)

    notes: List[str] = []
    notes.append("sprawdzono %d bibliotek ELF w %s" % (len(binaries), dist_dir))
    for library, users in sorted(absent_on_host.items()):
        notes.append("OSTRZEŻENIE (brak w tym systemie, nie dotyczy Windows): "
                     "%s <- %s" % (library, ", ".join(sorted(users)[:3])))
    if removed_by_us:
        for library, users in sorted(removed_by_us.items()):
            notes.append("BŁĄD: usunięto %s, a używa go %s"
                         % (library, ", ".join(sorted(users)[:3])))
        return False, notes
    if not absent_on_host:
        notes.append("domknięcie zależności OK")
    return True, notes


def run(cmd: List[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=PROJECT_ROOT)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Core2Chat")
    parser.add_argument("--mode", choices=("release", "debug", "onefile"),
                        default="release")
    parser.add_argument("--check", action="store_true",
                        help="validate spec + scan for secrets, do not build")
    parser.add_argument("--verify-dist", action="store_true",
                        help="check the dynamic dependency closure of dist/")
    args = parser.parse_args()

    if args.verify_dist:
        ok, notes = verify_dist(os.path.join(PROJECT_ROOT, "dist", "Core2Chat"))
        for note in notes:
            print("verify-dist: %s" % note)
        return 0 if ok else 4

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
    ok, notes = verify_dist(os.path.join(PROJECT_ROOT, "dist", "Core2Chat"))
    for note in notes:
        print("verify-dist: %s" % note)
    if not ok:
        print("ABORT: usunięto bibliotekę, której coś jeszcze używa.")
        return 4
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
