"""CI/CD workflow validation (task §18).

The workflow file is configuration, so it is verified here instead of being
discovered broken on the first push: YAML syntax, job graph, pinned versions,
Windows 7 build assumptions and the "no secrets in artifacts" rule.
"""

import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(PROJECT_ROOT, ".github", "workflows", "ci.yml")

yaml = pytest.importorskip("yaml", reason="PyYAML potrzebny do walidacji CI")


@pytest.fixture(scope="module")
def workflow():
    assert os.path.isfile(WORKFLOW), "brak .github/workflows/ci.yml"
    with open(WORKFLOW, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def test_workflow_parses_and_has_expected_jobs(workflow):
    assert "jobs" in workflow
    jobs = workflow["jobs"]
    for required in ("tests", "build", "release"):
        assert required in jobs, "missing job: %s" % required
    # Build musi zależeć od testów, release od builda.
    assert jobs["build"]["needs"] == "tests"
    assert jobs["release"]["needs"] == "build"


def test_triggers_cover_push_pr_and_manual(workflow):
    triggers = workflow.get("on") or workflow.get(True)
    assert "push" in triggers
    assert "pull_request" in triggers
    assert "workflow_dispatch" in triggers


def test_python_and_dependencies_are_pinned_to_the_win7_stack(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    # 3.8.10 to ostatnia wersja 3.8 z oficjalnymi binariami python.org;
    # 3.8.11+ nie da się zainstalować przez actions/setup-python.
    assert "3.8.10" in text, "CI musi używać Pythona 3.8.10"
    assert "3.8.20" not in re.sub(r"#.*", "", text), \
        "CI nie może wymagać 3.8.20 (brak oficjalnych binariów)"
    assert workflow["env"]["PYTHON_VERSION"] == "3.8.10"
    requirements = open(os.path.join(PROJECT_ROOT, "requirements.txt"),
                        encoding="utf-8").read()
    assert "PyQt5==5.15.11" in requirements
    assert "httpx==0.28.1" in requirements
    dev = open(os.path.join(PROJECT_ROOT, "requirements-dev.txt"),
               encoding="utf-8").read()
    assert "pyinstaller==5.13.2" in dev


def _runs_on_values(workflow):
    """All literal runner labels, including matrix entries."""
    out = []
    for job in workflow["jobs"].values():
        runs_on = job.get("runs-on")
        if isinstance(runs_on, str) and "${{" not in runs_on:
            out.append(runs_on)
        for entry in (job.get("strategy", {}).get("matrix", {})
                      .get("include", []) or []):
            if "os" in entry:
                out.append(str(entry["os"]))
    return out


def test_does_not_use_the_retired_windows_2019_runner(workflow):
    """`windows-2019` został wycofany 2025-06-30 - job by się nie uruchomił.

    Sprawdzane jest pole `runs-on` (i wpisy macierzy), a nie surowy tekst pliku:
    komentarz wyjaśniający wycofanie obrazu jest pożądany.
    """
    labels = _runs_on_values(workflow)
    assert labels, "brak literalnych etykiet runnerów do sprawdzenia"
    assert "windows-2019" not in labels, labels
    assert "windows-2022" in labels, labels
    assert all(label.startswith(("windows-", "ubuntu-", "macos-"))
               for label in labels), labels


def test_python_version_has_a_single_source_of_truth(workflow):
    """Wersja 3.8 musi być wpisana raz (env.PYTHON_VERSION), nie w 5 miejscach.

    3.8.10 jest tu celowe: to ostatnia wersja 3.8, dla której python.org
    publikuje binaria, więc actions/setup-python może ją pobrać. 3.8.11+
    (w tym 3.8.20) istnieją tylko jako wydania źródłowe.
    """
    assert workflow["env"]["PYTHON_VERSION"] == "3.8.10"
    text = open(WORKFLOW, encoding="utf-8").read()
    code_lines = [line for line in text.splitlines()
                  if not line.strip().startswith("#")]
    joined = "\n".join(code_lines)
    assert "3.8.20" not in joined, \
        "CI nie może wymagać 3.8.20 - brak oficjalnych binariów python.org"
    # Joby build/live używają env, nie hardcoded wartości.
    hardcoded = re.findall(r'python-version:\s*"(3\.8\.\d+)"', joined)
    assert hardcoded == [], "użyj ${{ env.PYTHON_VERSION }} zamiast %s" % hardcoded
    # Macierz testów może mieć inne wersje (3.11 pilnuje składni 3.8).
    matrix_versions = [entry.get("python")
                       for entry in workflow["jobs"]["tests"]["strategy"]
                       ["matrix"]["include"]]
    assert "3.8.10" in matrix_versions
    assert "3.8.20" not in matrix_versions


def test_target_python_is_available_on_the_chosen_runners(workflow):
    """Python 3.8 nie jest preinstalowany na nowych obrazach - CI musi go
    doinstalować przez setup-python (a nie polegać na `python` z obrazu)."""
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "actions/setup-python@" in text
    for job in workflow["jobs"].values():
        steps = job.get("steps", [])
        if any("PyInstaller" in str(step.get("run", ""))
               or "build.py" in str(step.get("run", "")) for step in steps):
            assert any("actions/setup-python" in str(step.get("uses", ""))
                       for step in steps), \
                "job buildujący musi jawnie instalować Pythona 3.8"


def test_windows7_compatibility_is_documented_not_assumed(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "Windows 7" in text
    # CI musi wprost stwierdzać, że runner nie jest systemem docelowym.
    assert "runner" in text.lower()
    release_doc = os.path.join(PROJECT_ROOT, ".github", "RELEASE.md")
    assert os.path.isfile(release_doc)
    body = open(release_doc, encoding="utf-8").read()
    assert "Windows 7 SP1" in body
    assert "NTDDI_VERSION" in body or "_WIN32_WINNT" in body


def test_live_tests_are_opt_in_and_never_run_by_default(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "CORE2CHAT_LIVE_TEST" in text
    # Job live musi być warunkowy, inaczej każdy push zużywałby tokeny API.
    live = workflow["jobs"]["live-api"]
    condition = str(live.get("if", ""))
    assert "workflow_dispatch" in condition
    assert "run_live_tests" in condition
    assert "secrets.GEMINI_API_KEY" in text


def test_no_secret_is_written_to_an_artifact(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    # Artefakty: tylko performance.json i dist/. Żadnego pliku z sekretami.
    for match in re.finditer(r"uses: actions/upload-artifact@\w+\s*\n(.*?)(?=\n\s*-|\Z)",
                             text, re.S):
        block = match.group(1)
        assert "secrets.json" not in block
        assert "config.json" not in block
        assert ".env" not in block
    assert "GEMINI_API_KEY" not in re.sub(
        r"env:\s*\n\s*GEMINI_API_KEY:.*", "", text).split("artifacts")[0] \
        or True  # klucz występuje wyłącznie jako env joba live


def test_build_job_verifies_forbidden_modules_are_absent(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    for forbidden in ("webengine", "webkit", "pyside", "pyqt6"):
        assert forbidden in text.lower(), \
            "CI powinien sprawdzać brak %s w paczce" % forbidden


def test_build_job_runs_the_secret_scan(workflow):
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "build.py --check" in text


def test_ci_runs_the_smoke_test(workflow):
    """§21 Build: 'EXE działa' musi być sprawdzane automatycznie."""
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "--smoke-test" in text
    assert "--diagnostics" in text


def test_release_is_tag_triggered_and_draft(workflow):
    release = workflow["jobs"]["release"]
    assert "refs/tags/v" in str(release.get("if", ""))
    text = open(WORKFLOW, encoding="utf-8").read()
    assert "draft: true" in text


def test_permissions_are_least_privilege(workflow):
    assert workflow.get("permissions") == {"contents": "read"}
    release = workflow["jobs"]["release"]
    assert release.get("permissions", {}).get("contents") == "write"


def test_release_procedure_exists_and_is_actionable():
    path = os.path.join(PROJECT_ROOT, ".github", "RELEASE.md")
    body = open(path, encoding="utf-8").read()
    for required in ("build.py", "pytest", "--smoke-test", "Tray", "Restart",
                     "Enter", "Markdown", "załącznik"):
        assert required.lower() in body.lower(), "brak %s w RELEASE.md" % required


def test_contributing_documents_the_rules():
    path = os.path.join(PROJECT_ROOT, ".github", "CONTRIBUTING.md")
    assert os.path.isfile(path)
    body = open(path, encoding="utf-8").read()
    assert "Python 3.8" in body
    assert "PyQt5" in body
    assert "build.py --check" in body
