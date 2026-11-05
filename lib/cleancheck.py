"""Every job that modifies a disk is followed by a clean step - and only where one can run.

    python3 lib/cleancheck.py          # check, non-zero on a problem
    python3 lib/cleancheck.py -v       # list every job and why it was classified that way

WHY THIS EXISTS

Cleaning is done over ssh: lib/clean_disk.clean_running() connects to a running guest and
runs the system's own release mechanism (`fstrim`, `zpool trim`, `Optimize-Volume`,
`diskutil apfs trim`). A disk that is not running, or that has no sshd, cannot be cleaned by
that mechanism at all.

That makes the rule two-sided, and both sides have already been got wrong:

    a job that modifies a disk and has NO clean step      the toolchain stayed on the image
                                                          with its package cache, and nothing
                                                          in the run said so

    a job that has no sshd and DOES have a clean step      the step cannot work: it would
                                                          boot the disk, fail to connect, and
                                                          report a broken clean where the
                                                          truth is "this stage has no ssh yet"

So the check needs to know, per job, whether ssh is available - and the answer is not a
property of the workflow, it is a property of what that job's guest has installed.

WHERE SSH COMES FROM, PER SYSTEM

    linux     the answer file installs openssh-server during the bake, so the FIRST job
              that produces a disk (bake) already has it
    freebsd   installerconfig installs sshd during the bake, same
    nixos     configuration-minimal.nix has services.openssh, so `base` has it
    windows   NOTHING installs sshd until windows/11/ssh.py runs. `base` bakes an installed
              Windows with no ssh at all
    macos     ditto: macos/15/ssh.py is what turns Remote Login on. setup and desktop are
              before it, and have no ssh

WHICH IS WHY THE EXPECTATION IS DECLARED HERE RATHER THAN INFERRED. Inferring "does this job
have ssh" from the steps' names would be a guess about an installer's contents. The table
below says, per workflow and job, whether a clean belongs there - and the reason, so a new
job is a decision rather than an omission.
"""

from __future__ import annotations

import glob
import os
import sys

import yaml

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLEAN_STEP = "clean the disk"


def log(message: str) -> None:
    print(f"[cleancheck] {message}", flush=True)


# EXPECTED CLEAN, PER JOB. True means "this job modifies a disk AND can reach it over ssh";
# False means "this job modifies a disk but has no sshd yet, so a clean step would fail".
#
# A job that is not listed is not checked: launchers (launch_*), publishers, selftest and the
# image build do not produce a disk.
EXPECTED = {
    # sshd is installed by the installer itself, so the very first job can clean.
    "ubuntu.yml":      {"bake": True,  "dev": True},
    "kubuntu.yml":     {"bake": True,  "dev": True},
    "freebsd.yml":     {"bake": True,  "dev": True},
    # services.openssh is in configuration-minimal.nix.
    "nixos.yml":       {"base": True,  "derive": True, "dev": True},
    # NO SSH UNTIL windows/11/ssh.py HAS RUN. `base` is a full Windows install with no sshd,
    # which is what makes the next job's `install ssh` necessary in the first place.
    "windows-11.yml":  {"base": None,  "ssh": True, "dev": True},
    # NO SSH UNTIL macos/15/ssh.py HAS RUN, and it is the ssh JOB that calls it. setup and
    # desktop both finish with a machine that has never had Remote Login on.
    #
    # THE SSH JOB HAS NO CLEAN STEP OF ITS OWN, BECAUSE IT CANNOT HAVE ONE. A separate step
    # gets a fresh machine: it would boot the disk from cold and wait for sshd to answer
    # again, and on macOS that is a different question from "is Remote Login on" - run
    # 42062472207's clean waited its whole 900 seconds and got
    #
    #     kex_exchange_identification: read: Connection reset by peer
    #
    # on a disk whose ssh stage had just passed wait_for_ssh with SSH-2.0-OpenSSH_9.9. So the
    # clean runs INSIDE macos/15/ssh.py, while that stage still holds the running guest and
    # the open connection - which is what it did before 6d57bc3 moved it out, and what
    # lib/clean_disk.clean_running's own docstring says the caller must do ("this does not
    # start or stop anything"). The same file drives both macOS workflows, so macos26's ssh
    # job is the same shape and this entry says so for both.
    "macos15.yml":     {"setup": None,  "desktop": None,  "ssh": False, "dev": True},
    "macos26.yml":     {"setup": None,  "desktop": None,  "ssh": False},
    # android.yml BAKE MAKES AN AVD, not a disk: a directory of raw images driven by the
    # emulator, which has no sshd and is not something fstrim or trim or ReTrim can reach.
    "android.yml":     {"bake": None},
}


def clean_index(steps: list[dict]) -> int | None:
    for i, step in enumerate(steps):
        if (step.get("name") or "").strip().lower() == CLEAN_STEP:
            return i
    return None


def push_index(steps: list[dict]) -> int | None:
    for i, step in enumerate(steps):
        if "push" in (step.get("name") or "").lower():
            return i
    return None


def main(argv: list[str]) -> int:
    verbose = "-v" in argv or "--verbose" in argv
    problems = 0
    checked = 0

    for path in sorted(glob.glob(os.path.join(REPO, ".github", "workflows", "*.yml"))):
        wf = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError) as exc:
            log(f"  CANNOT READ {wf}: {exc}")
            problems += 1
            continue

        wanted = EXPECTED.get(wf)
        if not wanted:
            continue

        jobs = doc.get("jobs") or {}
        for job_name, want in sorted(wanted.items()):
            job = jobs.get(job_name)
            if job is None:
                log(f"  MISSING JOB  {wf}: {job_name} - the table expects it")
                problems += 1
                continue

            steps = job.get("steps") or []
            checked += 1
            at = clean_index(steps)
            push = push_index(steps)

            # THREE STATES, NOT TWO. `True` and `None` were both spelled False in the first
            # version of this table, which merged two situations that need opposite handling:
            #
            #     None   this job has no sshd at all, so a clean step CANNOT work
            #     False  this job HAS sshd, and its stage cleans the disk itself, INSIDE the
            #            stage that owns the running guest - so a step must NOT be added
            #
            # Without the distinction, macOS's ssh job had to be written False and the table
            # stopped saying why, which is exactly how 6d57bc3 came to move that clean out and
            # break it (run 42062472207 waited 900s for sshd on a disk whose clean could only
            # run while the machine was up).
            if want is True and at is None:
                log(f"  NO CLEAN     {wf}: {job_name} modifies a disk and has ssh, "
                    f"but no '{CLEAN_STEP}' step")
                problems += 1
                continue

            if want is False and at is not None:
                log(f"  CLEAN MOVED  {wf}: {job_name} cleans inside its own stage, while it "
                    f"holds the running guest - a '{CLEAN_STEP}' step boots the disk from "
                    f"cold and has to reach sshd again, which is a different question")
                problems += 1
                continue

            if want is None and at is not None:
                log(f"  CANNOT CLEAN {wf}: {job_name} has a '{CLEAN_STEP}' step but no sshd "
                    f"at the end of this job - the clean would boot the disk, fail to "
                    f"connect, and read as a broken clean")
                problems += 1
                continue

            if at is not None and push is not None and at > push:
                log(f"  TOO LATE     {wf}: {job_name} cleans AFTER pushing the disk, so the "
                    f"published artifact is the uncleaned one")
                problems += 1
                continue

            if verbose:
                if at is not None:
                    where = "clean before push"
                elif want is False:
                    where = "clean inside the stage"
                else:
                    where = "no clean (no ssh yet)"
                log(f"  ok           {wf}: {job_name} - {where}")

    if problems:
        log(f"=== {problems} PROBLEM(S) ===")
        return 1
    log(f"=== {checked} JOB(S) CLASSIFIED CORRECTLY ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
