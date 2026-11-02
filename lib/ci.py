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
import urllib.error
import urllib.request

ORAS_VERSION = "1.2.2"


def log(msg: str) -> None:
    print(msg, flush=True)


class Failed(RuntimeError):
    pass


# --- kvm ---------------------------------------------------------------------


def check_kvm() -> None:
    """A bake needs /dev/kvm, and there is no software fallback worth pretending about.

    qemu's TCG works, and a Windows install that takes 40 minutes on KVM takes the
    better part of a day on it - past every timeout any of these workflows sets. So
    this fails here, where the message is one line, rather than in the bake as
    "timed out after 5400s".

    THE ACCESS CHECK IS AN OPEN, NOT os.access.

    /dev/kvm is 0o660 and owned by a group; on a GitHub runner the job's uid is not the
    owner and os.access() reports no write permission even though the process is in
    that group. The first version of this failed every bake on a runner where KVM
    worked perfectly:

        /dev/kvm: mode 0o660, char device 10:232
        FAILED: /dev/kvm is not readable and writable by uid 1001

    Opening it R/W is the only check that reflects what qemu will do.
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

    try:
        with open(path, "r+b"):
            pass
    except OSError as exc:
        # The device node exists but this uid cannot use it. On a GitHub runner the
        # node is 0o660 root:kvm and the job's uid is neither.
        #
        # sudo chmod 666 is the standard fix and is safe here: /dev/kvm is a character
        # device that only grants the ability to create VMs the kernel is already
        # willing to run, and the job that can reach this code can already run qemu.
        # Refusing instead would make every bake on a stock runner fail in prepare.
        log(f"{path} is not accessible to uid {os.getuid()} ({exc}); granting 0666")
        # Try a plain chmod first: in a container job the process is root and the
        # device arrives via --device, so no escalation is needed and sudo may not even
        # be installed. A host runner is the other way round.
        if os.access(path, os.W_OK):
            os.chmod(path, 0o666)
            result = None
        elif shutil.which("sudo"):
            result = subprocess.run(
                ["sudo", "chmod", "0666", path], capture_output=True, text=True,
            )
        else:
            raise Failed(
                f"{path} cannot be opened by uid {os.getuid()}, and neither a direct\n"
                f"chmod nor sudo is available. The runner needs to expose /dev/kvm; on\n"
                f"a container job that is `options: --device /dev/kvm`."
            ) from exc

        if result is not None and result.returncode != 0:
            raise Failed(
                f"{path} cannot be opened by uid {os.getuid()} and could not be "
                f"made accessible:\n{result.stderr.strip()[:300]}\n"
                f"The runner needs to expose /dev/kvm; on a container job that is\n"
                f"`options: --device /dev/kvm`."
            ) from exc

        try:
            with open(path, "r+b"):
                pass
        except OSError as second:
            raise Failed(
                f"{path} is still not accessible after chmod 0666: {second}"
            ) from second

    log("kvm: usable")


# --- disk --------------------------------------------------------------------

# PRUNE used to live here: fourteen paths of preinstalled toolchains, copied from
# dockur/windows. It is gone because it was measured rather than inherited - the runner
# had 121 GB free against a 30 GB requirement, and two of those paths are used by
# Actions itself. See free_disk() for the numbers.


def free_disk(need_gb: int) -> None:
    """Check there is room, and refuse early if there is not.

    WHY THIS NO LONGER DELETES ANYTHING

    It used to prune the preinstalled toolchains - dotnet, android, ghcup, the hosted
    toolcache - copied from dockur/windows, which does it because its job runs inside a
    container and genuinely cannot see them.

    Measured here instead of inherited: the runner had 121 GB free, against a 30 GB
    requirement. Every one of those `rm -rf` calls was deleting data nothing needed to
    delete, and two of the paths - /opt/hostedtoolcache and /usr/local/lib/node_modules
    - are used by Actions itself. Deleting them to free space that was never short is
    how a job develops a strange failure two steps later.

    What is kept is the check, because the number that matters is not "how much can we
    free" but "is there enough":

        7.2 GiB   the Windows ISO
        64 GiB    the qcow2, sparse, so a few GB of real blocks
        ~4 GiB    the compressed copy that gets pushed

    A runner that fails this check would fail the bake ninety minutes in with ENOSPC,
    which is worse than refusing at second twelve.
    """
    usage = shutil.disk_usage("/")
    available = usage.free // (1024 ** 3)
    log(f"available: {available} GB (need {need_gb})")

    if available < need_gb:
        raise Failed(
            f"at least {need_gb} GB is required, {available} GB is free.\n"
            f"Nothing was deleted: this repository used to prune the runner's\n"
            f"toolchains, and on a runner with this much free space that was noise.\n"
            f"If a runner really is this small, run `docker system prune -a` there or\n"
            f"use a larger runner."
        )


# --- oras --------------------------------------------------------------------


def install_oras() -> None:
    """Fetch oras into /usr/local/bin, idempotently.

    A disk is a file, not a container image, so it is pushed with oras rather than
    docker - the same choice dockur-macos made in publish-golden.yml, and for the same
    reason: oras stores arbitrary files as OCI artifacts without inventing a
    Dockerfile for them.
    """
    if shutil.which("oras"):
        probe = subprocess.run(["oras", "version"], capture_output=True, text=True)
        first = probe.stdout.splitlines()[0] if probe.stdout else "unknown version"
        log(f"oras already installed: {first}")
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

    # In a container job this runs as root and /usr/local/bin is writable directly.
    # On a host runner it is not, and sudo exists. Detecting that is cheaper than
    # requiring sudo everywhere - the bake image does not even ship it, which cost
    # three jobs a FileNotFoundError before the distinction was written down.
    target = "/usr/local/bin"
    binary = os.path.join(staging, "oras")
    if os.access(target, os.W_OK):
        shutil.copyfile(binary, os.path.join(target, "oras"))
        os.chmod(os.path.join(target, "oras"), 0o755)
    else:
        if not shutil.which("sudo"):
            raise Failed(
                f"{target} is not writable and sudo is not available.\n"
                f"Inside the bake image this step runs as root; on a host runner it\n"
                f"needs sudo. Neither is true here, which means the job is running as\n"
                f"an unprivileged user in an image that does not ship sudo."
            )
        subprocess.run(["sudo", "install", "-m", "0755", binary, f"{target}/oras"], check=True)

    shutil.rmtree(staging, ignore_errors=True)

    result = subprocess.run(["oras", "version"], capture_output=True, text=True)
    if result.returncode != 0:
        raise Failed(f"oras installed but does not run: {result.stderr[:400]}")
    log(result.stdout.strip())


# --- registry ----------------------------------------------------------------


def login(registry: str) -> None:
    """Log in to a registry for the tools that will push to it.

    ORAS FIRST, docker SECOND.

    Inside the bake image there is no docker CLI - the image is the environment qemu
    runs in, not a build host - so `docker login` fails with

        FileNotFoundError: [Errno 2] No such file or directory: 'docker'

    oras carries its own login and writes the same credential store, so it covers the
    case that matters here: everything this repository pushes is a bare file pushed
    with oras. docker is tried second for a host runner, where the CLI exists and an
    image build may need the session.

    The token is fed through stdin rather than a shell pipe: `echo "$TOKEN" | docker
    login` puts it where any process in the job can read it.
    """
    token = os.environ.get("GITHUB_TOKEN")
    actor = os.environ.get("GITHUB_ACTOR")
    if not token or not actor:
        raise Failed("GITHUB_TOKEN and GITHUB_ACTOR must be set")

    attempts = []
    if shutil.which("oras"):
        attempts.append((["oras", "login", registry, "--username", actor,
                          "--password-stdin"], "oras"))
    if shutil.which("docker"):
        attempts.append((["docker", "login", registry, "--username", actor,
                          "--password-stdin"], "docker"))

    if not attempts:
        raise Failed(
            "neither oras nor docker is installed, so nothing can log in to "
            f"{registry}.\nThe image carries oras; see scripts/install in lib/ci.py."
        )

    errors = []
    for argv, name in attempts:
        result = subprocess.run(argv, input=token.encode(), capture_output=True)
        if result.returncode == 0:
            log(f"logged in to {registry} as {actor} (via {name})")
            return
        errors.append(f"{name}: {result.stderr.decode('utf-8', 'replace')[:300]}")

    raise Failed(f"could not log in to {registry}:\n" + "\n".join(errors))


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


def select(wanted: str, release: str, name: str, output_key: str = "run") -> bool:
    """Should this matrix leg run?

    The workflow used to answer this in a shell `case` over a comma-joined list, which
    is the kind of string matching that works until somebody passes a name with a
    space. Comma-separated values, trimmed, matched against either the release number
    or its marketing name; "all" matches everything.

    Writes `key=true|false` to $GITHUB_OUTPUT, because that is how a step's result
    reaches the next step's `if:`.
    """
    tokens = {t.strip().lower() for t in wanted.split(",") if t.strip()}
    hit = "all" in tokens or release.strip().lower() in tokens or name.strip().lower() in tokens
    value = "true" if hit else "false"
    log(f"release {release} ({name}): wanted={sorted(tokens)} -> {value}")

    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"{output_key}={value}\n")
    return hit


def append_summary(text: str) -> None:
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

    A failure is raised, not swallowed. This used to return [] on any error, which made
    "the package does not exist" and "the request failed" look identical - and the
    caller prints the result, so a broken lookup was reported as "no tags".

    An empty package is not a failure: ghcr answers a package with no tags with
    {"tags": null}, which is a successful response with no tags in it.
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
    except urllib.error.HTTPError as exc:
        raise Failed(
            f"could not list the tags of {repo} in {registry}: HTTP {exc.code}"
        ) from exc
    except urllib.error.URLError as exc:
        raise Failed(f"could not reach {registry}: {exc.reason}") from exc

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
    tg.add_argument("--repo", default=None,
                    help="owner/name; defaults to $GITHUB_REPOSITORY")

    rc = sub.add_parser("repo", help="print the lowercased owner/repo")

    sel = sub.add_parser("select", help="matrix gate: should this leg run?")
    sel.add_argument("--wanted", required=True, help="comma-separated numbers or names")
    sel.add_argument("--release", required=True)
    sel.add_argument("--name", required=True)
    sel.add_argument("--output-key", default="run")

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
            repo = (args.repo or lowercase_repository()).lower()
            for t in tags_in_package(args.registry, repo, args.prefix):
                print(t)
        elif args.cmd == "repo":
            print(lowercase_repository())
        elif args.cmd == "select":
            select(args.wanted, args.release, args.name, args.output_key)
    except Failed as exc:
        log(f"FAILED: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
