"""S2, S10 and S12 through the fake adapter: each suite finds the misbehaviour it exists for, and only that."""
from __future__ import annotations

import json
import platform
import shutil

import pytest

from cwabench.container import merge_strace, parse_strace
from cwabench.runner import run
from cwabench.suites.s12_goldens import accept
from cwabench.validate import validate_run

QUICK_S2 = """
[s2]
repetitions = 3
cell_repetitions = 1
cells = {cells}
container = false
"""


def _rows(run_dir, suite):
    path = run_dir / "suites" / suite / "results.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _findings(run_dir, suite):
    path = run_dir / "suites" / suite / "findings.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _s2(fake_config, mode, cells):
    config = fake_config(mode, suites=("S2",), extra=QUICK_S2.format(cells=json.dumps(cells)))
    run_dir, status = run(config, build=False, log=lambda _: None)
    assert validate_run(run_dir) == []
    return run_dir, status


def test_s2_passes_a_deterministic_adapter(fake_config):
    run_dir, status = _s2(fake_config, "oracle", ["baseline", "tz:Asia/Kathmandu", "locale:tr_TR.UTF-8", "env:minimal",
                                                  "home:unset", "cwd:empty", "burst:32"])
    assert status == "pass"
    assert _findings(run_dir, "S2") == []
    assert all(r["matches_reference"] and all(v is not False for v in r["matches_reference"].values())
               for r in _rows(run_dir, "S2"))


def test_s2_catches_random_nondeterminism_in_the_baseline(fake_config):
    run_dir, status = _s2(fake_config, "flaky", ["baseline"])
    assert status == "fail"
    findings = _findings(run_dir, "S2")
    assert findings and {f["checks"][0] for f in findings} == {"baseline:trace"}


def test_s2_blames_only_the_cell_that_changes_the_answer(fake_config):
    run_dir, status = _s2(fake_config, "tz", ["baseline", "tz:UTC", "tz:Asia/Kathmandu", "locale:C"])
    assert status == "fail"
    assert {f["checks"][0] for f in _findings(run_dir, "S2")} == {"tz:Asia/Kathmandu:trace"}
    differing = {r["env_cell"] for r in _rows(run_dir, "S2") if r["finding"]}
    assert differing == {"tz:Asia/Kathmandu"}
    assert all(r["trace"] for r in _rows(run_dir, "S2") if r["finding"])  # differing traces are kept as blobs


def test_s2_reports_a_fault_in_an_environment_cell(fake_config):
    run_dir, status = _s2(fake_config, "needs-home", ["baseline", "home:unset"])
    assert status == "fail"
    assert {f["checks"][0] for f in _findings(run_dir, "S2")} == {"home:unset:decision"}
    assert all(r["outcome"] == "crashed" and r["stderr"] for r in _rows(run_dir, "S2") if r["env_cell"] == "home:unset")


@pytest.mark.skipif(platform.system() != "Darwin" or not shutil.which("sandbox-exec"), reason="needs macOS sandbox-exec")
def test_s10_catches_an_adapter_that_needs_the_network(fake_config):
    config = fake_config("network", suites=("S10",), extra="[s10]\nmacos_sandbox = true\n")
    run_dir, status = run(config, build=False, log=lambda _: None)
    assert status == "fail"
    rows = _rows(run_dir, "S10")
    assert {r["outcome"] for r in rows if r["env_cell"] == "sandbox:no-network"} == {"crashed"}
    assert validate_run(run_dir) == []

    run_dir, status = run(fake_config("oracle", suites=("S10",)), build=False, log=lambda _: None)
    assert _findings(run_dir, "S10") == []


def test_s12_flags_regressions_against_adopted_goldens(fake_config, contract):
    baseline, status = run(fake_config("oracle", suites=("S1", "S12")), build=False, log=lambda _: None)
    assert status == "partial"  # nothing adopted yet
    config = fake_config("oracle", suites=("S1", "S12"))
    _, adopted = accept(config, baseline)
    assert adopted == len(contract.cases) + len(contract.rejections)

    same, status = run(fake_config("oracle", suites=("S1", "S12")), build=False, log=lambda _: None)
    assert status == "pass"
    assert {r["drift"] for r in _rows(same, "S12")} == {"match"}

    broken, status = run(fake_config("drop-excluded", suites=("S1", "S12")), build=False, log=lambda _: None)
    regressed = {r["case_id"] for r in _rows(broken, "S12") if r["drift"] == "regression"}
    assert regressed == {c.id for c in contract.cases if c.expected_trace.get("excluded")}
    assert status == "fail" and validate_run(broken) == []


def test_s12_needs_s1(fake_config):
    from cwabench.config import ConfigError

    with pytest.raises(ConfigError, match="needs one of them"):
        fake_config("oracle", suites=("S12",))


STRACE = """\
101 execve("/opt/cwa/bin/a", ["a"], 0x0 /* 3 vars */) = 0
101 openat(AT_FDCWD, "/etc/ld.so.cache", O_RDONLY|O_CLOEXEC) = 3
101 openat(AT_FDCWD, "/etc/localtime", O_RDONLY) = 3
101 openat(AT_FDCWD, "/root/.config/x", O_RDONLY) = -1 ENOENT (No such file or directory)
102 socket(AF_INET, SOCK_STREAM|SOCK_CLOEXEC, IPPROTO_TCP <unfinished ...>
101 socketpair(AF_UNIX, SOCK_STREAM, 0, [4, 5]) = 0
102 <... socket resumed>) = 6
102 connect(6, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("1.2.3.4")}, 16) = -1 ENETUNREACH (Network is unreachable)
101 openat(AT_FDCWD, "/usr/lib/python3/__pycache__/x.pyc.123", O_WRONLY|O_CREAT|O_EXCL, 0644) = -1 EROFS (Read-only file system)
101 openat(AT_FDCWD, "/tmp/out.json", O_WRONLY|O_CREAT|O_TRUNC, 0644) = 7
101 execve("/bin/sh", ["sh"], 0x0 /* 3 vars */) = 0
"""


def test_strace_parsing():
    report = parse_strace(STRACE)
    assert report["network"] == ["socket(AF_INET) = 6", "connect(AF_INET) = -1 ENETUNREACH"]
    assert report["local_sockets"] == {"socketpair(AF_UNIX)": 1}
    assert report["reads"] == {"/etc/localtime (ok)": 1, "/root/.config/x (ENOENT)": 1}
    assert report["writes"] == {"/tmp/out.json (ok)": 1}
    assert list(report["cache_writes"]) == ["/usr/lib/python3/__pycache__/ (EROFS)"]
    merged = merge_strace([report, report])
    assert merged["child_processes"] == [{"event": "/bin/sh", "count": 2}]
    assert merged["network"][1] == {"event": "socket(AF_INET) = 6", "count": 2}
