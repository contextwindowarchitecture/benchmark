"""The Linux container: S2's platform and clock cells and S10's isolation cells run the four assemblers in it
(domain-1-plan.md, 7.2 and 7.10).

Variants (`[container.variants.<name>]`) build the same Containerfile for another platform (`platform`, e.g.
linux/amd64, emulated on another host) or with other base images (`images`, e.g. an older Python): S2's platform and
toolchain matrix. A variant's image is tagged by its own context hash, which includes its platform and images.

The build context holds each assembler's working tree (tracked and untracked files, git-ignored ones left out), the
same tree the host builds, so the two platforms run the same code. The image tag is a hash of the context, so an
unchanged context reuses its image and a changed one can never be mistaken for it.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from . import adapters as adapters_mod
from .adapters import Invocation
from .config import Config
from .corpora import Snapshot
from .determinism import Answer, signature

ROOT = Path(__file__).resolve().parent.parent
IMAGE = "localhost/cwa-bench-d1"

# Where each adapter lives in the image (container/Containerfile), and the probe that tells whether a clock shift
# reaches its runtime.
ADAPTERS = {
    "python": {"argv": ["/opt/cwa/python-venv/bin/python", "/opt/cwa/adapters/python_adapter.py"], "env": {},
               "probe": ["/opt/cwa/python-venv/bin/python", "/opt/cwa/probes/clock.py"]},
    "typescript": {"argv": ["/usr/local/bin/node", "/opt/cwa/adapters/typescript_adapter.mjs"],
                   "env": {"CWA_TS_ASSEMBLER": "/opt/cwa/typescript/dist/index.js"},
                   "probe": ["/usr/local/bin/node", "/opt/cwa/probes/clock.mjs"]},
    "go": {"argv": ["/opt/cwa/bin/cwa-adapter-go"], "env": {}, "probe": ["/opt/cwa/probes/clock-go"]},
    "rust": {"argv": ["/opt/cwa/bin/cwa-adapter-rust"], "env": {}, "probe": ["/opt/cwa/probes/clock-rust"]},
}

THIRTY_YEARS = "10950d"
_ISOLATED_FLAGS = ["--network", "none", "--read-only", "--tmpfs", "/tmp", "--cap-add", "SYS_PTRACE",
                   "--security-opt", "seccomp=unconfined"]


@dataclass(frozen=True)
class ContainerCell:
    name: str
    description: str
    env: dict = field(default_factory=dict)
    faketime: str | None = None
    strace: bool = False


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    flags: list[str]
    base_env: dict
    cells: list[ContainerCell]


PROFILES = {
    "linux": Profile(
        "linux", "An ordinary container: network on, writable root, HOME set",
        [], {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/root", "LANG": "C.UTF-8"},
        [ContainerCell("linux:baseline", "Linux, the container's ordinary environment"),
         ContainerCell("linux:faketime-30y", "Wall clock 30 years earlier (libfaketime)", faketime=f"-{THIRTY_YEARS}"),
         ContainerCell("linux:faketime+30y", "Wall clock 30 years later (libfaketime)", faketime=f"+{THIRTY_YEARS}")]),
    "linux-isolated": Profile(
        "linux-isolated", "No network namespace, read-only root, empty /tmp, no usable HOME",
        _ISOLATED_FLAGS, {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent", "LANG": "C.UTF-8"},
        [ContainerCell("linux-isolated:baseline", "Linux with no network and a read-only root"),
         ContainerCell("linux-isolated:strace", "As linux-isolated, every network, file and exec syscall traced",
                       strace=True)]),
}


class ContainerError(Exception):
    pass


@dataclass(frozen=True)
class Variant:
    name: str
    platform: str | None  # e.g. linux/amd64; None for the engine's own
    images: dict  # overrides of [container.images]
    description: str

    @property
    def cell(self) -> str:
        return f"platform:{self.platform}" if self.platform and not self.images else f"toolchain:{self.name}"

    def profile(self) -> Profile:
        base = PROFILES["linux"]
        return Profile(f"variant-{self.name}", self.description, [], base.base_env,
                       [ContainerCell(self.cell, self.description)])


def variant(config: Config, name: str) -> Variant:
    table = config.settings.get("container", {}).get("variants", {}).get(name)
    if not isinstance(table, dict):
        raise ContainerError(f"no [container.variants.{name}] table")
    images = table.get("images", {})
    unknown = sorted(set(images) - {"go", "rust", "node", "python", "uv"})
    if unknown:
        raise ContainerError(f"[container.variants.{name}].images names unknown stages: {', '.join(unknown)}")
    return Variant(name, table.get("platform"), dict(images), str(table.get("description", name)))


def _images(config: Config, chosen: Variant | None) -> dict:
    return {**config.settings.get("container", {}).get("images", {}), **(chosen.images if chosen else {})}


def engine(config: Config) -> str:
    name = config.settings.get("container", {}).get("engine", "podman")
    path = shutil.which(name)
    if path is None:
        raise ContainerError(f"{name} is not installed")
    return path


def _run(argv: list[str], timeout: float = 3600, **kwargs) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        raise ContainerError(f"{' '.join(argv[:3])} … did not finish in {timeout:.0f} s") from None


def _tree(checkout: Path) -> list[str]:
    done = _run(["git", "-C", str(checkout), "ls-files", "-z", "-co", "--exclude-standard"])
    if done.returncode != 0:
        raise ContainerError(f"cannot list the files of {checkout}")
    return sorted(p for p in done.stdout.decode().split("\0") if p and (checkout / p).is_file())


def _bench_files() -> list[tuple[Path, str]]:
    files = [(ROOT / "container" / "runner.py", "bench/runner.py"),
             (ROOT / "container" / "rust-build.sh", "bench/rust-build.sh")]
    files += [(p, f"bench/probes/{p.name}") for p in sorted((ROOT / "container" / "probes").iterdir()) if p.is_file()]
    files += [(p, f"bench/adapters/{p.name}") for p in sorted((ROOT / "adapters").iterdir()) if p.is_file()]
    files.append((ROOT / "container" / "Containerfile", "Containerfile"))
    return files


def context_files(config: Config) -> list[tuple[Path, str]]:
    """Every file the image is built from, as (source, path in the context)."""
    files = []
    for name in ADAPTERS:
        adapter = config.adapters.get(name) or config.all_adapters.get(name)
        if adapter is None:
            raise ContainerError(f"the container needs an [adapters.{name}] table for its checkout")
        files += [(adapter.checkout / p, f"{name}/{p}") for p in _tree(adapter.checkout)]
    return files + _bench_files()


def context_digest(config: Config, files: list[tuple[Path, str]], chosen: Variant | None = None) -> str:
    digest = hashlib.sha256()
    for key, value in sorted(_images(config, chosen).items()):
        digest.update(f"{key}={value}\0".encode())
    if chosen is not None and chosen.platform:
        digest.update(f"platform={chosen.platform}\0".encode())
    for source, target in sorted(files, key=lambda f: f[1]):
        digest.update(target.encode() + b"\0" + hashlib.sha256(source.read_bytes()).digest())
    return digest.hexdigest()


def build_image(config: Config, log, chosen: Variant | None = None) -> dict:
    """Build the image (or a variant's) unless one from the same context exists; describe it either way."""
    podman = engine(config)
    files = context_files(config)
    digest = context_digest(config, files, chosen)
    tag = f"{IMAGE}:{digest[:16]}"
    built = False
    if _run([podman, "image", "exists", tag]).returncode != 0:
        context = config.build_dir / "container" / "context"
        if context.exists():
            shutil.rmtree(context)
        for source, target in files:
            (context / target).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, context / target)
        args = []
        for key, value in sorted(_images(config, chosen).items()):
            args += ["--build-arg", f"{key.upper()}_IMAGE={value}"]
        if chosen is not None and chosen.platform:
            args += ["--platform", chosen.platform]
        what = f" ({chosen.name})" if chosen else ""
        log(f"container: building {tag}{what} from {len(files)} files (this takes a while the first time)")
        done = _run([podman, "build", "-t", tag, "-f", str(context / "Containerfile"), *args, str(context)],
                    timeout=4 * 3600)
        if done.returncode != 0:
            tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-25:]
            raise ContainerError("image build failed:\n" + "\n".join(tail))
        built = True
    inspect = _run([podman, "image", "inspect", tag])
    info = json.loads(inspect.stdout or b"[{}]")[0] if inspect.returncode == 0 else {}
    return {
        "tag": tag,
        "id": info.get("Id"),
        "context_sha256": digest,
        "context_files": len(files),
        "platform": f"{info.get('Os', 'linux')}/{info.get('Architecture', '?')}",
        "built_this_run": built,
        "variant": chosen.name if chosen else None,
        "run_platform": chosen.platform if chosen else None,
        "images": _images(config, chosen),
    }


def run_profile(config: Config, image: dict, profile: Profile, corpus: list[Snapshot], adapters: list[str],
                repetitions: dict[str, int], log) -> tuple[list[Answer], dict, dict]:
    """Run the profile's cells in one container. Returns the answers, the clock probes and the toolchain versions."""
    podman = engine(config)
    names = [a for a in adapters if a in ADAPTERS]
    work = config.build_dir / "container" / "work" / profile.name
    if work.exists():
        shutil.rmtree(work)
    (work / "snapshots").mkdir(parents=True)
    for index, snapshot in enumerate(corpus):
        (work / "snapshots" / f"{index}.json").write_bytes(snapshot.data)
    job = {
        "snapshots": [{"index": i, "path": f"snapshots/{i}.json"} for i in range(len(corpus))],
        "adapters": {n: ADAPTERS[n] for n in names},
        "cells": [{"name": c.name, "repetitions": repetitions.get(c.name, 1), "env": c.env, "faketime": c.faketime,
                   "strace": c.strace} for c in profile.cells],
        "timeout": config.timeout_s,
        "concurrency": config.concurrency,
        "base_env": profile.base_env,
    }
    (work / "job.json").write_text(json.dumps(job), encoding="utf-8")
    total = sum(j["repetitions"] for j in job["cells"]) * len(names) * len(corpus)
    log(f"container: {profile.name}: {total} invocations")
    platform = ["--platform", image["run_platform"]] if image.get("run_platform") else []
    argv = [podman, "run", "--rm", *platform, *profile.flags, "-v", f"{work}:/work", image["tag"],
            "python3", "/opt/cwa/runner.py", "/work/job.json"]
    done = _run(argv, timeout=max(600, config.timeout_s * total / max(1, config.concurrency) * 2))
    if done.returncode != 0:
        tail = done.stderr.decode("utf-8", "replace").strip().splitlines()[-25:]
        raise ContainerError(f"{profile.name} run failed:\n" + "\n".join(tail))

    out = work / "out"
    probes = json.loads((out / "probes.json").read_text(encoding="utf-8"))
    versions = json.loads((out / "versions.json").read_text(encoding="utf-8"))
    answers = []
    for line in (out / "results.jsonl").read_text(encoding="utf-8").splitlines():
        raw = json.loads(line)
        invocation = Invocation(raw["exit_code"], (out / raw["stdout"]).read_bytes(),
                                (out / raw["stderr"]).read_bytes(), raw["wall_ms"], raw["timed_out"])
        outcome = adapters_mod.classify(invocation)
        answer = Answer(corpus[raw["snapshot"]], raw["adapter"], raw["cell"], image["platform"], raw["repetition"],
                        invocation, outcome, signature(outcome), raw["applied"], raw["not_applied_reason"])
        answer.strace = (out / raw["strace"]) if raw["strace"] else None
        answers.append(answer)
    answers.sort(key=lambda a: (a.cell, a.adapter, a.repetition, a.snapshot.digest))
    return answers, probes, versions


# strace ---------------------------------------------------------------------------------------------------------------

_LINE = re.compile(r"^(?:\[pid\s+)?(\d+)\]?\s+(.*)$")
_CALL = re.compile(r"^([a-z0-9_]+)\((.*)\)\s+=\s+(-?\d+|\?)(?:\s+(\w+))?")
_FAMILY = re.compile(r"\b(AF_[A-Z0-9]+)\b")
_PATH = re.compile(r'"((?:[^"\\]|\\.)*)"')
IP_FAMILIES = ("AF_INET", "AF_INET6", "AF_PACKET")
NETWORK_CALLS = ("socket", "connect", "bind", "sendto", "sendmsg", "sendmmsg", "listen")
FILE_CALLS = ("open", "openat", "openat2")
READ_ALLOWED = ("/usr/", "/lib/", "/lib64/", "/opt/cwa/", "/proc/", "/sys/", "/dev/", "/etc/ld.so.cache")
WRITE_ALLOWED = ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "/dev/urandom", "/dev/random")


def parse_strace(text: str) -> dict:
    """Network, file and exec activity in one strace -f log."""
    pending: dict[str, str] = {}
    calls = []
    for raw in text.splitlines():
        match = _LINE.match(raw.strip())
        if not match:
            continue
        pid, rest = match.groups()
        if rest.endswith("<unfinished ...>"):
            pending[pid] = rest[: -len("<unfinished ...>")].rstrip()
            continue
        resumed = re.match(r"^<\.\.\. ([a-z0-9_]+) resumed>(.*)$", rest)
        if resumed:
            rest = pending.pop(pid, resumed.group(1) + "(") + resumed.group(2)
        call = _CALL.match(rest)
        if call:
            calls.append(call.groups())

    network, local, reads, writes, caches, execs = [], Counter(), Counter(), Counter(), Counter(), []
    for name, args, result, errno in calls:
        if name in NETWORK_CALLS or name == "socketpair":
            families = _FAMILY.findall(args)
            family = families[0] if families else None
            if name != "socketpair" and family in IP_FAMILIES:
                network.append(f"{name}({family}) = {result}{' ' + errno if errno else ''}")
            else:
                local[f"{name}({family or '?'})"] += 1
        elif name in FILE_CALLS:
            paths = _PATH.findall(args)
            if not paths:
                continue
            path = paths[0]
            writing = any(flag in args for flag in ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC"))
            ok = result != "?" and not result.startswith("-")
            if writing and "/__pycache__/" in path:
                # The interpreter caching bytecode for a module it imports, not the assembler writing anything.
                caches[f"{path.rsplit('/__pycache__/', 1)[0]}/__pycache__/ ({'ok' if ok else errno})"] += 1
            elif writing and not path.startswith(WRITE_ALLOWED):
                writes[f"{path} ({'ok' if ok else errno})"] += 1
            elif not writing and not path.startswith(READ_ALLOWED):
                reads[f"{path} ({'ok' if ok else errno})"] += 1
        elif name == "execve":
            paths = _PATH.findall(args)
            execs.append(paths[0] if paths else "?")
    return {"network": network, "local_sockets": dict(local), "reads": dict(reads), "writes": dict(writes),
            "cache_writes": dict(caches), "execs": execs, "calls": len(calls)}


def merge_strace(reports: list[dict]) -> dict:
    """Combine one adapter's per-invocation reports: counts summed, each distinct event listed once."""
    network, local, reads, writes, caches, execs = Counter(), Counter(), Counter(), Counter(), Counter(), Counter()
    for report in reports:
        caches.update(report["cache_writes"])
        network.update(report["network"])
        local.update(report["local_sockets"])
        reads.update(report["reads"])
        writes.update(report["writes"])
        execs.update(set(report["execs"][1:]))  # the first execve starts the adapter itself
    return {
        "invocations": len(reports),
        "network": [{"event": k, "count": v} for k, v in sorted(network.items())],
        "local_sockets": [{"event": k, "count": v} for k, v in sorted(local.items())],
        "reads": [{"event": k, "count": v} for k, v in sorted(reads.items())],
        "writes": [{"event": k, "count": v} for k, v in sorted(writes.items())],
        "cache_writes": [{"event": k, "count": v} for k, v in sorted(caches.items())],
        "child_processes": [{"event": k, "count": v} for k, v in sorted(execs.items())],
    }


def group(answers: list[Answer]) -> dict[str, list[Answer]]:
    out: dict[str, list[Answer]] = defaultdict(list)
    for answer in answers:
        out[answer.cell].append(answer)
    return out
