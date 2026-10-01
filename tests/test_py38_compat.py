"""Python 3.8 compatibility guard (specification §1, rules 21-23).

The target runtime is Python 3.8 on Windows 7 (CI uses 3.8.10, the last 3.8
release with official python.org binaries; a local unofficial 3.8.20 build also
works). Development may happen on a newer interpreter, so this test statically
rejects syntax and stdlib calls that would break 3.8 at runtime.
"""

import ast
import json
import os
import re
import sys
from typing import List, Set, Tuple

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKIP_DIRS = {".git", ".qwen", "__pycache__", "build", "dist", ".venv",
             "node_modules", "assets"}

# stdlib callables introduced after 3.8, as (receiver_name, attribute) pairs.
# Only explicit receivers are flagged so that unrelated attributes such as
# ``self.cache`` (our own repository) are not false positives.
BANNED_CALLS: Set[Tuple[str, str]] = {
    ("functools", "cache"),          # 3.9 - use lru_cache(maxsize=None)
    ("asyncio", "to_thread"),        # 3.9
    ("itertools", "pairwise"),       # 3.10
    ("math", "lcm"),                 # 3.9
}
BANNED_METHOD_NAMES: Set[str] = {
    "removeprefix", "removesuffix",  # str methods, 3.9
}
BANNED_MODULES: Set[str] = {
    "zoneinfo", "graphlib", "tomllib", "exceptiongroup", "typing_extensions",
}
BUILTIN_GENERICS: Set[str] = {"list", "dict", "set", "frozenset", "tuple",
                              "type"}


def python_files() -> List[str]:
    out: List[str] = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            if name.endswith(".py"):
                out.append(os.path.join(root, name))
    assert out, "no python files found - test would silently pass"
    return sorted(out)


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def test_every_module_parses_with_python_38_grammar():
    failures: List[str] = []
    for path in python_files():
        try:
            ast.parse(read(path), filename=path, feature_version=(3, 8))
        except SyntaxError as exc:
            failures.append("%s:%s %s" % (os.path.relpath(path, PROJECT_ROOT),
                                         exc.lineno, exc.msg))
    assert not failures, "Python 3.9+ syntax detected:\n" + "\n".join(failures)


def _annotations(node: ast.AST) -> List[ast.AST]:
    out: List[ast.AST] = []
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        out.append(node.returns if node.returns is not None else ast.Pass())
        for arg in list(node.args.args) + list(node.args.kwonlyargs):
            if arg.annotation is not None:
                out.append(arg.annotation)
        for arg in [node.args.vararg, node.args.kwarg]:
            if arg is not None and arg.annotation is not None:
                out.append(arg.annotation)
    elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
        out.append(node.annotation)
    return out


def test_no_builtin_generic_subscripts_in_annotations():
    """``list[str]`` / ``dict[str, int]`` require Python 3.9+."""
    failures: List[str] = []
    for path in python_files():
        tree = ast.parse(read(path), filename=path)
        for node in ast.walk(tree):
            for annotation in _annotations(node):
                for sub in ast.walk(annotation):
                    if isinstance(sub, ast.Subscript) \
                            and isinstance(sub.value, ast.Name) \
                            and sub.value.id in BUILTIN_GENERICS:
                        failures.append("%s:%d %s[...]" % (
                            os.path.relpath(path, PROJECT_ROOT),
                            getattr(sub, "lineno", 0), sub.value.id))
    assert not failures, "use typing.List/Dict/Set/Tuple:\n" + "\n".join(failures)


def test_no_pep604_union_annotations():
    """``int | None`` requires Python 3.10+; use Optional[int]."""
    failures: List[str] = []
    for path in python_files():
        tree = ast.parse(read(path), filename=path)
        for node in ast.walk(tree):
            for annotation in _annotations(node):
                for sub in ast.walk(annotation):
                    if isinstance(sub, ast.BinOp) and isinstance(sub.op,
                                                                 ast.BitOr):
                        failures.append("%s:%d" % (
                            os.path.relpath(path, PROJECT_ROOT),
                            getattr(sub, "lineno", 0)))
    assert not failures, "PEP 604 unions detected:\n" + "\n".join(failures)


def test_no_match_statements():
    for path in python_files():
        tree = ast.parse(read(path), filename=path)
        for node in ast.walk(tree):
            assert not isinstance(node, getattr(ast, "Match", ())), \
                "match statement (3.10+) in %s" % path


def test_no_post_38_stdlib_usage():
    failures: List[str] = []
    for path in python_files():
        tree = ast.parse(read(path), filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in BANNED_METHOD_NAMES:
                    failures.append("%s:%d .%s()" % (
                        os.path.relpath(path, PROJECT_ROOT), node.lineno,
                        node.attr))
                if isinstance(node.value, ast.Name) and \
                        (node.value.id, node.attr) in BANNED_CALLS:
                    failures.append("%s:%d %s.%s" % (
                        os.path.relpath(path, PROJECT_ROOT), node.lineno,
                        node.value.id, node.attr))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in BANNED_MODULES:
                        failures.append("%s:%d import %s" % (
                            os.path.relpath(path, PROJECT_ROOT), node.lineno,
                            alias.name))
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.split(".")[0] in BANNED_MODULES:
                    failures.append("%s:%d from %s" % (
                        os.path.relpath(path, PROJECT_ROOT), node.lineno,
                        node.module))
    assert not failures, "post-3.8 stdlib usage:\n" + "\n".join(failures)


def test_forbidden_gui_dependencies_are_not_imported():
    """QtWebEngine / PySide / PyQt6 are banned by the specification."""
    banned = ("PyQt6", "PySide2", "PySide6", "QtWebEngine", "QtWebKit",
              "electron", "qasync", "google.genai", "google.generativeai")
    failures: List[str] = []
    for path in python_files():
        source = read(path)
        tree = ast.parse(source, filename=path)
        for node in ast.walk(tree):
            names: List[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if any(name.startswith(b) for b in banned):
                    failures.append("%s:%d %s" % (
                        os.path.relpath(path, PROJECT_ROOT), node.lineno, name))
    assert not failures, "forbidden dependency imported:\n" + "\n".join(failures)


def test_requirements_pin_the_target_versions():
    path = os.path.join(PROJECT_ROOT, "requirements.txt")
    if not os.path.isfile(path):
        pytest.skip("requirements.txt not created yet")
    content = read(path)
    assert "PyQt5==5.15.11" in content
    assert "httpx==0.28.1" in content
    assert "Pygments==2.17.2" in content
    # Only requirement lines matter: comments legitimately *name* the forbidden
    # stacks while explaining the policy. The runtime trio is closed: PyQt5
    # (GUI), httpx (HTTP), Pygments (syntax highlighting, user-approved 0.1.2).
    packages = [line.strip() for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
                and not line.strip().startswith("-r")]
    assert sorted(packages) == ["PyQt5==5.15.11", "Pygments==2.17.2",
                                "httpx==0.28.1"], packages
    for banned in ("PyQt6", "PySide", "QtWebEngine", "qasync"):
        assert not any(pkg.lower().startswith(banned.lower())
                       for pkg in packages)


def test_runtime_dependency_count_stays_minimal():
    """Dependency policy §68: keep the runtime footprint small."""
    path = os.path.join(PROJECT_ROOT, "requirements.txt")
    if not os.path.isfile(path):
        pytest.skip("requirements.txt not created yet")
    packages = [line.strip() for line in read(path).splitlines()
                if line.strip() and not line.strip().startswith("#")
                and not line.strip().startswith("-r")]
    assert len(packages) <= 4, "runtime deps grew: %s" % packages
    pinned = dict(item.split("==") for item in packages if "==" in item)
    assert pinned.get("Pygments") == "2.17.2", (
        "kolorowanie składni wymaga Pygments (utils/highlight.backend() bez "
        "niego degraduje do 'plain' - kod bez kolorów); pin: %r"
        % pinned.get("Pygments"))


def test_build_script_scans_working_tree_and_history():
    """`build.py --check` must pass on a clean tree (secrets + spec syntax)."""
    import subprocess

    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "build.py"), "--check"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "secret scan (working tree): clean" in result.stdout
    assert "secret scan (git history): clean" in result.stdout
    assert "spec syntax: OK" in result.stdout


def test_bundle_trimming_lists_are_consistent():
    """pyinstaller.spec and build.py must agree on what is dropped.

    If the two lists drift, --verify-dist reports a library as "removed by us"
    while the spec still ships it (or the other way round), and the check stops
    being meaningful.
    """
    import subprocess

    script = (
        "import ast, json, sys\n"
        "src = open('pyinstaller.spec', encoding='utf-8').read()\n"
        "tree = ast.parse(src)\n"
        "found = {}\n"
        "for node in tree.body:\n"
        "    if isinstance(node, ast.Assign):\n"
        "        for target in node.targets:\n"
        "            if isinstance(target, ast.Name):\n"
        "                try:\n"
        "                    found[target.id] = ast.literal_eval(node.value)\n"
        "                except ValueError:\n"
        "                    pass\n"
        "print(json.dumps({k: sorted(v) for k, v in found.items()\n"
        "                  if isinstance(v, tuple)}))\n")
    completed = subprocess.run([sys.executable, "-c", script],
                               cwd=PROJECT_ROOT, capture_output=True,
                               text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr[-800:]
    spec_lists = json.loads(completed.stdout)

    assert "UNWANTED_QT_LIBS" in spec_lists
    assert "UNWANTED_PYQT5_PREFIXES" in spec_lists
    assert "UNWANTED_QT_PLUGIN_DIRS" in spec_lists
    assert "UNWANTED_QT_PLUGIN_FILES" in spec_lists

    sys.path.insert(0, PROJECT_ROOT)
    import importlib

    build_module = importlib.import_module("build")
    for list_name in ("UNWANTED_QT_LIBS", "UNWANTED_QT_PLUGIN_FILES"):
        spec_entries = set(spec_lists[list_name])
        build_entries = set(getattr(build_module, list_name))
        assert spec_entries == build_entries, (
            "%s się rozjechała: tylko w spec=%s, tylko w build.py=%s"
            % (list_name, sorted(spec_entries - build_entries)[:5],
               sorted(build_entries - spec_entries)[:5]))


def test_bundle_keeps_windows_platform_and_icon_plugins():
    """Na Windows paczka musi zawierać qwindows i qico - bez nich EXE nie wstanie."""
    spec_text = open(os.path.join(PROJECT_ROOT, "pyinstaller.spec"),
                     encoding="utf-8").read()
    dropped_files = re.findall(
        r"UNWANTED_QT_PLUGIN_FILES = \((.*?)\)", spec_text, re.S)[0]
    dropped_dirs = re.findall(
        r"UNWANTED_QT_PLUGIN_DIRS = \((.*?)\)", spec_text, re.S)[0]
    for required in ("qwindows", "qico", "qjpeg"):
        assert required not in dropped_files, \
            "wtyczka %s nie może być usunięta" % required
    for required in ("platforms", "imageformats", "styles"):
        assert '"%s"' % required not in dropped_dirs, \
            "katalog wtyczek %s nie może być usunięty" % required


def test_bundle_keeps_the_modules_the_app_actually_imports():
    """Trimming must not remove anything QtCore/QtGui/QtWidgets needs."""
    spec_text = open(os.path.join(PROJECT_ROOT, "pyinstaller.spec"),
                     encoding="utf-8").read()
    for required in ("QtCore", "QtGui", "QtWidgets"):
        assert "PyQt5/%s" % required not in spec_text, \
            "%s zostałby wycięty z paczki" % required
    # Platformy i formaty obrazów muszą zostać (QIcon, qwindows/qxcb).
    for kept in ("platforms", "imageformats", "iconengines"):
        assert '"%s"' % kept not in re.findall(
            r"UNWANTED_QT_PLUGIN_DIRS = \((.*?)\)", spec_text, re.S)[0], \
            "katalog wtyczek %s nie może być usunięty" % kept


def test_verify_dist_reports_missing_bundle(tmp_path):
    sys.path.insert(0, PROJECT_ROOT)
    import importlib

    build_module = importlib.import_module("build")
    ok, notes = build_module.verify_dist(str(tmp_path / "nope"))
    assert ok is False
    assert any("missing" in note for note in notes)
