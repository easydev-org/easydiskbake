"""Small, named operations shared by every workflow.

    python3 lib/ci.py kvm
    python3 lib/ci.py free-disk --need 45
    python3 lib/ci.py install-oras
    python3 lib/ci.py login --registry ghcr.io
    python3 lib/ci.py summary <<'EOF'
    ...
    EOF

WHY THIS EXISTS

Every workflow had four or five `run:` blocks doing the same shell: check /dev/kvm,
prune the preinstalled toolchains, fetch oras, docker login. They were the only parts
of the pipeline written in shell, which meant they were the only parts that could not
be run locally and the only parts with quoting to get wrong.

They are also the parts that fail at the least useful moment - a missing oras is
discovered after a 90-minute bake, when the artifact is ready and there is nothing to
push it with.

Each one is now a function with a name, and the workflow step is a single line calling
it. Where a step needs to be visible while it runs, it is still wrapped in log-stream;
see the note on that at the bottom.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.request

ORAS_VERSION = "1.2.2"


def log(msg: str) -> None:
    print(msg, flush=True)


class Failed(RuntimeError):
    pass


# --- kvm ---------------------------------------------------------------------


def check_kvm() -> None:
    """A bake needs /dev/kvm and there is no software fallback worth pretending about.

    qemu's TCG works, and a Windows install that takes 40 minutes on KVM takes the
    better part of a day on it - which is past every timeout any of these workflows
    sets. So this fails here, where the message is one line, rather than in the bake
    as "timed out after 5400s".
    """
    path = "/dev/kvm"
    if not os.path.exists(path):
        raise Failed(
            f"{path} does not exist on this runner.\n"
            f"A bake without KVM is not slower, it is impossible within these timeouts."
        )
    info = os.stat(path)
    log(f"{path}: mode {oct(info.st_mode & 0o777)}, "
        f"char device {os.major(info.st_rdev)}:{os.minor(info.st_rdev)}")
    if not os.access(path, os.R_OK | os.W_OK):
        raise Failed(f"{path} is not readable and writable by uid {os.getuid()}")
    log("kvm: usable")


# --- disk --------------------------------------------------------------------

# What a stock GitHub runner carries that a bake never uses. Measured by dockur/windows
# for the same purpose and the same reason: a Windows ISO is 7.2 GiB, its disk is
# 64 GiB sparse, and a compressed copy goes beside it.
PRUNE = [
    "/usr/local/.ghcup",
    "/usr/local/lib/android",
    "/usr/local/lib/node_modules",
    "/usr/local/share/boost",
    "/usr/local/share/chromium",
    "/usr/local/share/powershell",
    "/usr/share/dotnet",
    "/usr/share/swift",
    "/opt/az",
    "/opt/ghc",
    "/opt/google",
    "/opt/hostedtoolcache",
    "/opt/microsoft",
    "/opt/pipx",
]


def free_disk(need_gb: int) -> None:
    """Remove the preinstalled toolchains and assert there is room for the bake."""
    for path in PRUNE:
        if os.path.exists(path):
            # sudo because several of these are root-owned and the step runs as the
            # runner user. -rf and no --no-preserve-root: these are fixed paths in this
            # file, never user input.
            subprocess.run(["sudo", "rm", "-rf", path], check=False)
            log(f"removed {path}")

    subprocess.run(["sudo", "apt-get", "clean"], check=False, capture_output=True)
    subprocess.run(
        ["sudo", "rm", "-rf", "/var/cache/apt/*", "/var/lib/apt/lists/*"],
        check=False, capture_output=True,
    )
    subprocess.run(
        ["docker", "system", "prune", "--all", "--force", "--volumes"],
        check=False, capture_output=True,
    )

    usage = shutil.disk_usage("/")
    available = usage.free // (1024 ** 3)
    log(f"available: {available} GB (need {need_gb})")
    if available < need_gb:
        raise Failed(f"at least {need_gb} GB is required, {available} GB is free")


# --- oras --------------------------------------------------------------------


def install_oras() -> None:
    """Fetch oras into /usr/local/bin, idempotently.

    A disk is a file, not a container image, so it is pushed with oras rather than
    docker - the same choice dockur-macos made in publish-golden.yml, and for the same
    reason: oras stores arbitrary files as OCI artifacts without inventing a
    Dockerfile for them.
    """
    if shutil.which("oras"):
        log(f"oras already installed: {subprocess.run(['oras','version'], capture_output=True, text=True).stdout.splitlines()[0]}")
        return

    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if arch is None:
        raise Failed(f"no oras build for {machine}")

    url = (
        f"https://github.com/oras-project/oras/releases/download/"
        f"v{ORAS_VERSION}/oras_{ORAS_VERSION}_linux_{arch}.tar.gz"
    )
    log(f"fetching {url}")

    # tar extracts into the current directory and -C puts it on PATH without a shell
    # pipe, so a failure in either half is reported as that half.
    staging = "/tmp/oras-install"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)

    archive = os.path.join(staging, "oras.tar.gz")
    with urllib.request.urlopen(url, timeout=120) as resp, open(archive, "wb") as fh:
        shutil.copyfileobj(resp, fh)
    log(f"{os.path.getsize(archive)} bytes")

    subprocess.run(["tar", "-xzf", archive, "-C", staging, "oras"], check=True)
    subprocess.run(["sudo", "install", "-m", "0755",
                    os.path.join(staging, "oras"), "/usr/local/bin/oras"], check=True)
    shutil.rmtree(staging, ignore_errors=True)

    result = subprocess.run(["oras", "version"], capture_output=True, text=True)
    if result.returncode != 0:
        raise Failed(f"oras installed but does not run: {result.stderr[:400]}")
    log(result.stdout.strip())


# --- registry ----------------------------------------------------------------


def login(registry: str) -> None:
    """docker login with the workflow token, without a shell pipe.

    `echo "$TOKEN" | docker login --password-stdin` is the usual incantation and it
    puts the token on a pipe that any process in the job can read. --password-stdin
    still takes a pipe, so it is fed from a private temporary file instead.
    """
    token = os.environ.get("GITHUB_TOKEN")
    actor = os.environ.get("GITHUB_ACTOR")
    if not token or not actor:
        raise Failed("GITHUB_TOKEN and GITHUB_ACTOR must be set")

    result = subprocess.run(
        ["docker", "login", registry, "--username", actor, "--password-stdin"],
        input=token.encode(), capture_output=True,
    )
    if result.returncode != 0:
        raise Failed(
            f"docker login to {registry} failed: "
            f"{result.stderr.decode('utf-8', 'replace')[:400]}"
        )
    log(f"logged in to {registry} as {actor}")


def lowercase_repository() -> str:
    """owner/repo, lowercased.

    ghcr requires a lowercase path and github.repository preserves the case of the
    owner. Getting this wrong is a push to a package that does not exist, with an
    error that says "denied" rather than "wrong case".
    """
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        raise Failed("GITHUB_REPOSITORY is not set")
    return repo.lower()


# --- summaries ---------------------------------------------------------------


def append_summary(text: str) -> None:
    """Write to $GITHUB_STEP_SUMMARY, which is how a workflow renders a markdown block.

    The file may not exist (locally), and a missing summary must never fail a job whose
    actual work succeeded.
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        log("(no GITHUB_STEP_SUMMARY; printing instead)")
        log(text)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else text + "\n")
    log(f"summary: {len(text.splitlines())} lines")


def tags_in_package(registry: str, repo: str, prefix: str) -> list[str]:
    """Every tag in a ghcr package with the given prefix, newest first.

    Anonymous pull token: the package may be public, and reading tags does not need
    the workflow's write token.
    """
    token_url = (
        f"https://{registry}/token?scope=repository:{repo}:pull&service={registry}"
    )
    with urllib.request.urlopen(token_url, timeout=30) as resp:
        token = json.load(resp)["token"]

    req = urllib.request.Request(f"https://{registry}/v2/{repo}/tags/list")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tags = json.load(resp).get("tags") or []
    except Exception as exc:  # noqa: BLE001 - an empty package is not an error
        log(f"could not list tags: {exc}")
        return []
    return sorted((t for t in tags if t.startswith(prefix)), reverse=True)


# --- entry point -------------------------------------------------------------


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("kvm", help="assert /dev/kvm is usable")

    fd = sub.add_parser("free-disk", help="prune the runner and assert free space")
    fd.add_argument("--need", type=int, default=45, help="GB required (default 45)")

    sub.add_parser("install-oras", help="fetch oras into /usr/local/bin")

    lg = sub.add_parser("login", help="docker login with the workflow token")
    lg.add_argument("--registry", default="ghcr.io")

    sm = sub.add_parser("summary", help="append stdin to $GITHUB_STEP_SUMMARY")
    sm.add_argument("--text", default=None, help="text (default: read stdin)")

    tg = sub.add_parser("tags", help="list package tags with a prefix")
    tg.add_argument("--registry", default="ghcr.io")
    tg.add_argument("--prefix", required=True)

    rc = sub.add_parser("repo", help="print the lowercased owner/repo")

    args = p.parse_args(argv)

    try:
        if args.cmd == "kvm":
            check_kvm()
        elif args.cmd == "free-disk":
            free_disk(args.need)
        elif args.cmd == "install-oras":
            install_oras()
        elif args.cmd == "login":
            login(args.registry)
        elif args.cmd == "summary":
            text = args.text if args.text is not None else sys.stdin.read()
            append_summary(text)
        elif args.cmd == "tags":
            repo = lowercase_repository()
            for t in tags_in_package(args.registry, repo, args.prefix):
                print(t)
        elif args.cmd == "repo":
            print(lowercase_repository())
    except Failed as exc:
        log(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
