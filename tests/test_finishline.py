"""Finish-line static security and synthetic S3 negative cases; no network or sleeps."""
import ast
from copy import deepcopy
from pathlib import Path
import re
import subprocess

import pytest
import yaml

from bench import analysis

ROOT = Path(__file__).resolve().parents[1]


def unsafe_pattern():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    script = workflow["jobs"]["provider"]["steps"][-1]["run"]
    return script.split("--include='*.sh' '", 1)[1].split("' \"$directory\"", 1)[0]


def test_no_unsafe_calls_in_sources_bench_and_scripts():
    pattern = unsafe_pattern()
    files = [p for name in ("src", "bench", "scripts") for p in (ROOT / name).rglob("*")
             if p.suffix in (".py", ".sh")]
    assert files
    assert not [p.relative_to(ROOT).as_posix() for p in files if re.search(pattern, analysis.read(p).decode())]


@pytest.mark.parametrize("parts", [("ev", "al("), ("ex", "ec("), ("pi", "ckle"),
    ("mar", "shal"), ("os.sys", "tem("), ("shell=", "True"), ("yaml.lo", "ad(")])
def test_ci_pattern_detects_unsafe_constructs(parts):
    assert re.search(unsafe_pattern(), "".join(parts))


def credential_patterns():
    tree = ast.parse((ROOT / "tests/test_evidence.py").read_text())
    test = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "test_no_official_results_or_credentials")
    patterns = ast.literal_eval(next(n.value for n in test.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "patterns" for t in n.targets)))
    # No fixture/file exemptions. Synthetic admin-header names are not credential values;
    # the original evidence/log test still enforces its separate admin-field prohibition.
    return {name: pattern for name, pattern in patterns.items() if name != "admin-token"}


def test_tracked_tree_has_no_secret_patterns():
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    names = set(result.stdout.decode().strip("\0").split("\0"))
    # Include the authorized additions before staging, too; CI's tracked tree includes them.
    names.update(("bench/charts.py", "tests/test_finishline.py", "docs/mechanism-ladder.svg"))
    patterns = credential_patterns()
    assert len(patterns) == 5
    findings = []
    for name in sorted(names):
        assert name and not Path(name).is_absolute() and ".." not in Path(name).parts
        raw = analysis.read(ROOT / name)
        findings.extend((name, category) for category, pattern in patterns.items() if re.search(pattern, raw))
    assert not findings  # Report only paths/categories, never matching values.


@pytest.mark.parametrize("baseline,zero_source,expected,gate", [
    ("C0", "C0", "INCONCLUSIVE", False), ("C3", "C0", "INCONCLUSIVE", False),
    ("C3", "C3", "NO MEASURABLE EFFECT", True)])
@pytest.mark.parametrize("zero_rounds", [(0,), (0, 1, 2)])
def test_s3_rejects_zero_duplicate_baseline(baseline, zero_source, expected, gate, zero_rounds):
    def cell(duplicates):
        return dict(runs=[dict(round=i+1, seed=12001+i, metrics=dict(
            duplicate_provider_executions=value, success_fraction=1.0, extra_attempts=0))
            for i, value in enumerate(duplicates)])
    cells = {"S3/C0": cell([90]*3), "S3/C3": cell([90]*3), "S3/C4": cell([0]*3)}
    for i in zero_rounds:
        cells["S3/" + zero_source]["runs"][i]["metrics"]["duplicate_provider_executions"] = 0
    if baseline == "C0" and len(zero_rounds) == 3:
        assert all(r["metrics"]["duplicate_provider_executions"] == 0 for r in cells["S3/C0"]["runs"])
    decision = dict(metrics={"S3": dict(primary="duplicate_provider_executions")}, relative_threshold=.2)
    result = analysis.comparison("S3", baseline, "C4", deepcopy(cells), decision)
    assert result["efficacy_gate"] is gate and result["verdict"] == expected
    assert result["verdict"] != "EFFECTIVE"
