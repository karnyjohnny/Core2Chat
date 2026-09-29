"""Python 3.8 compatibility guard (specification §1, rules 21-23).

The target runtime is Python 3.8.20 on Windows 7. Development may happen on a
newer interpreter, so this test statically rejects syntax and stdlib calls that
would break 3.8 at runtime.
"""

import ast
import os
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
    # Only requirement lines matter: comments legitimately *name* the forbidden
    # stacks while explaining the policy.
    packages = [line.strip() for line in content.splitlines()
                if line.strip() and not line.strip().startswith("#")
                and not line.strip().startswith("-r")]
    assert sorted(packages) == ["PyQt5==5.15.11", "httpx==0.28.1"], packages
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
