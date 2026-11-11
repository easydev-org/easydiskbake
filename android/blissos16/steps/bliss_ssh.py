"""The host-side acts of putting sshd inside an Android guest.

    python: bliss_ssh:build_payload
    python: bliss_ssh:push_payload
    python: bliss_ssh:install_key
    python: bliss_ssh:start

WHY THESE ARE EXTENSIONS AND NOT STEPS

Everything else in android/blissos16/steps/ssh.yaml is a step: `adb_root:`, `adb:`, and the
`require_ssh:` that is the verdict. These four are not single actions on a guest - one downloads
fifteen packages and materialises their symlinks, one writes a config file whose every line is a
measured fact, one starts a daemon and reads its log - and none is expressible in the vocabulary
without inventing a step for each. `python:` is the escape hatch lib/steps.py documents.

EVERY COMMAND IN THIS FILE WAS RUN AGAINST A REAL GUEST. What is unusual about this stage is how
many of its facts are unguessable, and each one cost a nine-minute boot to find:

    /etc/passwd is empty and `/` is read-only     "invalid user root" from a guest whose account
                                                  exists nowhere
    bionic has no getspnam                       a correct $6$ hash in /etc/shadow is ignored, so
                                                  password auth cannot work at all
    Termux's sshd has bin/bash compiled in        "User root not allowed because shell .../bash
                                                  does not exist", whatever /etc/passwd says
    sshd requires 700 on the account's HOME       "bad ownership or modes for directory"
    six libraries the packages do not declare      each one: bind, accept, close
"""

from __future__ import annotations

import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for path in (REPO, os.path.join(REPO, "lib")):
    if path not in sys.path:
        sys.path.insert(0, path)

import adb  # noqa: E402
import android_ssh  # noqa: E402

# WHERE THE GUEST KEEPS IT. adb cannot push into /data/data directly - vold owns that directory
# and creates it on first boot - so the tree goes to the tmpfs and is copied from there.
STAGING = "/data/local/tmp/termux"
PREFIX = android_ssh.PREFIX
# THE ACCOUNT'S HOME, AND IT MUST BE 700. sshd refuses a key from a home anybody can write to:
#
#     Authentication refused: bad ownership or modes for directory ...
HOME = "/data/local/home"
SSHD_CONFIG = "/data/local/tmp/sshd_config"


def _work(driver) -> str:
    return getattr(driver, "work", "") or "."


def _values(driver) -> dict:
    return getattr(driver, "values", {}) or {}


def _serial(driver) -> str:
    return f"127.0.0.1:{_values(driver).get('adb_port') or '5555'}"


def _say(output: str, limit: int = 30) -> None:
    for line in (output or "").splitlines()[:limit]:
        print(f"    {line}", flush=True)


def build_payload(driver, step) -> bool:
    """Download the Termux packages and assemble the prefix as a directory of real files."""
    try:
        tree = android_ssh.build(_work(driver))
    except Exception as exc:                       # noqa: BLE001 - reported as a verdict
        print(f"  the payload could not be built: {exc}", flush=True)
        return False
    files = sum(len(fs) for _, _, fs in os.walk(tree))
    print(f"  {files} files in {tree}", flush=True)
    # THE PATH TRAVELS ON THE DRIVER rather than being re-derived: a second build() would download
    # fifteen packages again, and a path guessed in the file would be a second place to agree with
    # lib/android_ssh.py's naming.
    setattr(driver, "android_ssh_tree", tree)
    return files > 0


def push_payload(driver, step) -> bool:
    """Push the tree to the guest's tmpfs and copy it into the prefix, as root."""
    tree = getattr(driver, "android_ssh_tree", "")
    if not tree or not os.path.isdir(tree):
        print("  no payload to push; `python: bliss_ssh:build_payload` has to run first", flush=True)
        return False
    serial = _serial(driver)

    print(f"  pushing {tree} -> {STAGING}", flush=True)
    status, output = adb.run("push", tree + "/.", STAGING + "/", timeout=900, serial=serial)
    if status != 0:
        print(f"  push failed: {output[-400:]}", flush=True)
        return False
    # `adb push` REPORTS SUCCESS AND STILL PRINTS FAILURES FOR INDIVIDUAL FILES, which is how a
    # payload missing its libraries reads as a working install until sshd fails to link.
    if "failed" in output.lower():
        print(f"  push reported failures: {output[-400:]}", flush=True)
        return False

    # THE COPY IS THE INSTALL. `cp -a` rather than a rename because the two paths are different
    # filesystems - the tmpfs and the encrypted /data volume.
    move = (
        f"mkdir -p {PREFIX}; "
        f"cp -a {STAGING}/. {PREFIX}/; "
        f"chmod -R 755 {PREFIX}/bin {PREFIX}/libexec; "
        f"rm -rf {STAGING}; "
        f"test -x {PREFIX}/bin/sshd && test -x {PREFIX}/bin/bash && echo INSTALLED"
    )
    status, output = adb.shell(serial, move, timeout=600)
    print(f"  install -> {status}", flush=True)
    _say(output)
    # BASH IS CHECKED TOO, AND FOR A REASON THIS STAGE MEASURED: sshd has its path compiled in and
    # refuses a login without it, so a payload without bash is a daemon that starts and rejects
    # every account.
    if status != 0 or "INSTALLED" not in output:
        return False
    return True


def install_key(driver, step) -> bool:
    """Install the caller's public key as root's authorized key, with the modes sshd requires."""
    # THE KEY COMES FROM THE STEP, NOT FROM `driver.values`, AND THE DIFFERENCE IS A REAL ONE.
    #
    # `driver.values` is the resolved `vars` mapping, whose KEYS are the var names - but the walk
    # substitutes `{{ public_key }}` INTO the step before `_run_python` is called, so by the time
    # this runs the value is on the step and the var is not necessarily in `values` (it was read
    # from `args.public_key`, which is an override, not a var). Reading the wrong one produced
    #
    #     no --public-key was given
    #
    # for a run that HAD passed one. The step first, `values` only as a fallback.
    key = str(step.get("public_key") or "").strip()
    if not key:
        key = str(_values(driver).get("public_key") or "").strip()
    # `--public-key` NAMES A FILE, NOT THE KEY. The first version treated the whole argument as
    # the key material and installed a 14-byte authorized_keys containing the path:
    #
    #     -rw------- 1 root root 14 ... authorized_keys
    #     KEYED
    #
    # which reads as success and authenticates nobody - `authorized_keys` is a list of KEYS, and a
    # filename on one line is an invalid entry sshd skips without a word.
    if key and os.path.isfile(key):
        with open(key, encoding="utf-8") as fh:
            key = fh.read().strip()
    if not key or " " not in key:
        print(f"  {key[:60]!r} is not a public key (a .pub file holds "
              f"`<type> <base64> <comment>`)", flush=True)
        return False
    if not key:
        # A KEY IS A RUN FACT AND THE CALLER SUPPLIES ONE. Baking a key into the repository is how
        # a disk gets an account anybody reading the repository can log into.
        print("  no --public-key was given; this stage needs one because the guest cannot check "
              "a password (bionic has no getspnam). Generate one with `ssh-keygen -t ed25519` "
              "and pass the .pub file.", flush=True)
        return False

    serial = _serial(driver)
    work = _work(driver)
    os.makedirs(work, exist_ok=True)
    local = os.path.join(work, "authorized_keys")
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(key if key.endswith("\n") else key + "\n")

    status, output = adb.run("push", local, "/data/local/tmp/authorized_keys",
                             timeout=120, serial=serial)
    if status != 0:
        print(f"  push failed: {output[-300:]}", flush=True)
        return False

    # 700 ON THE HOME AND THE DIRECTORY, 600 ON THE FILE. sshd checks all three and refuses with
    # "bad ownership or modes for directory", which names the directory and not the fix.
    setup = (
        f"P={HOME}; mkdir -p $P/.ssh; "
        f"cp /data/local/tmp/authorized_keys $P/.ssh/authorized_keys; "
        f"chmod 700 $P $P/.ssh; chmod 600 $P/.ssh/authorized_keys; "
        f"chown -R root:root $P; "
        f"ls -ld $P $P/.ssh; ls -l $P/.ssh/authorized_keys; echo KEYED"
    )
    status, output = adb.shell(serial, setup, timeout=180)
    print(f"  key -> {status}", flush=True)
    _say(output)
    return status == 0 and "KEYED" in output


def install_boot_hook(driver, step) -> bool:
    """Make the guest start sshd by itself, so the PUBLISHED disk answers with no adb involved.

    THIS IS THE DIFFERENCE BETWEEN "THIS RUN HAS ssh" AND "THE DISK HAS ssh", and it was found by
    booting the artifact the stage had just published: the binary was there, correct and
    executable, and NOTHING WAS RUNNING.

        $ ls -la /data/local/sshd/bin/sshd
        -rwxr-xr-x 1 root root 426216 ... sshd
        $ ps -A | grep sshd
        (nothing)

    `/data` IS NOT ENCRYPTED ON THIS DISK, which the earlier Android-x86 route had led me to
    expect and which is worth stating because it is what makes a boot hook possible at all:

        /dev/block/vda2 on /data type ext4 (rw,seclabel,noatime)     no dm, no nosuid
        getprop ro.crypto.state -> unsupported

    Android's init is the thing that runs at boot, and `/system/etc/init/*.rc` is where it looks.
    The service is started `oneshot` after `boot` rather than as a long-running service: sshd
    forks its own children and init's supervision of a forking daemon is a second thing to get
    wrong. A tiny wrapper waits for the filesystem to be writable and then execs the daemon.
    """
    serial = _serial(driver)
    work = _work(driver)
    os.makedirs(work, exist_ok=True)

    wrapper = (
        "#!/system/bin/sh\n"
        "# Wait for the encrypted-data path to settle, then start sshd. `boot_completed` is the\n"
        "# one signal that says the framework is up and /data is mounted for everybody.\n"
        "for i in $(seq 1 120); do\n"
        "  [ \"$(getprop sys.boot_completed)\" = \"1\" ] && break\n"
        "  sleep 2\n"
        "done\n"
        f"P={PREFIX}\n"
        "export LD_LIBRARY_PATH=$P/lib\n"
        "mkdir -p $P/var/empty $P/var/run\n"
        # THE PATH sshd WAS COMPILED WITH, WHICH CANNOT BE MOVED. See `start()`.
        "mkdir -p /data/data/com.termux/files/usr/var/empty 2>/dev/null\n"
        "chmod 755 /data/data/com.termux/files/usr/var/empty 2>/dev/null\n"
        "T=/data/data/com.termux/files/usr\n"
        "mkdir -p $T/libexec $T/bin 2>/dev/null\n"
        # sshd's compiled-in default login shell, as a wrapper that sets LD_LIBRARY_PATH. See
        # `start()`: a copy of the binary cannot link.
        "printf '%s\\n' '#!/system/bin/sh' \\\n"
        "  'export LD_LIBRARY_PATH=$P/lib' \\\n"
        "  'exec $P/bin/bash \"$@\"' > $T/bin/bash 2>/dev/null\n"
        "chmod 755 $T/bin/bash 2>/dev/null\n"
        "mkdir -p /data/data/com.termux/files/home 2>/dev/null\n"
        "chmod 700 /data/data/com.termux/files/home 2>/dev/null\n"
        # sshd execs these for each connection, from the path it was BUILT with. See `start()`.
        "for f in $P/libexec/*; do\n"
        "  [ -f \"$f\" ] && [ ! -e \"$T/libexec/$(basename $f)\" ] && "
        "cp \"$f\" $T/libexec/ 2>/dev/null\n"
        "done\n"
        "chmod 755 $T/libexec/* 2>/dev/null\n"
        "chmod 755 $P/var/empty\n"
        # `exec` AND `-D`, BECAUSE A WRAPPER THAT EXITS TAKES ITS CHILD WITH IT.
        #
        # This is the last link in the chain and the one that cost the most runs. The wrapper ran
        # on every boot - `/data/local/tmp/sshd.err` was 26 KB of "Server listening" lines, one
        # per boot - and yet nothing was ever listening when the machine had finished starting.
        # The reason is a `oneshot` service: when its program exits, init kills the service's
        # PROCESS GROUP, so sshd - which the wrapper had forked and left running - was killed
        # a moment after it bound the port.
        #
        # `-D` keeps sshd in the foreground and `exec` makes it the service's own process, so the
        # thing init supervises IS the daemon. The wait for the framework stays above it.
        # AND THE WRAPPER REFUSES TO START FROM AN EMPTY CONFIG. If the file is missing or has no
        # content, sshd would run with every default - `StrictModes yes` against a /data that can
        # never satisfy it - and refuse every login while looking healthy:
        f"if [ ! -s {PREFIX}/etc/ssh/sshd_config.installed ]; then\n"
        f"  echo \"no persistent sshd config; refusing to start with defaults\"\n"
        f"  exit 1\n"
        f"fi\n"
        f"exec $P/bin/sshd -D -e -f {PREFIX}/etc/ssh/sshd_config.installed "
        f">> /data/local/tmp/sshd.err 2>&1\n"
    )
    rc = (
        "# Start sshd inside Android at boot. Written by android/blissos16/steps/ssh.yaml.\n"
        "#\n"
        "# THE WRAPPER IS IN /system/bin AND NOT IN /data/local/tmp, WHICH IS THE WHOLE BUG THIS\n"
        "# FIXES. The first version pointed init at /data/local/tmp/start-sshd.sh, and that is a\n"
        "# TMPFS: it is empty on the next boot, so init ran a service whose program did not exist\n"
        "# and said so only in its own log:\n"
        "#\n"
        "#     [init.svc.blissos_sshd]: [stopped]\n"
        "#     [ro.boottime.blissos_sshd]: [15732976153]     <- it DID try\n"
        "#     $ ls /data/local/tmp/start-sshd.sh\n"
        "#     No such file or directory\n"
        "#\n"
        "# A file that has to survive a power-off belongs on a filesystem that does.\n"
        "service blissos_sshd /system/bin/start-sshd.sh\n"
        # `class late_start` IS WHAT MAKES `on boot` START IT, AND IT NEEDS NO `disabled`.
        #
        # A service with `disabled` is started only by an explicit `start`, and the explicit
        # `start` here is the `on boot` trigger below - which is correct but fragile: one rc that
        # forgets the trigger and the daemon never runs. A service in a class is started when
        # init reaches that class, and `late_start` is the class that comes after the framework
        # - which is exactly when /data is usable and the networks are up.
        #
        # AND IT IS NOT `oneshot`. A oneshot service's process group is killed when its program
        # exits - which is what happened to the sshd the wrapper had forked: it bound 8022 and was
        # killed a moment later, on every boot:
        #
        #     $ wc -c /data/local/tmp/sshd.err
        #     26386                       <- one "Server listening" per boot, and no listener left
        #
        # The wrapper `exec`s `sshd -D` now, so the daemon IS the service process and there is
        # nothing to kill. init restarts it if it ever dies, which is what a server wants.
        "    class late_start\n"
        "    user root\n"
        "    group root\n"
        "\n"
        # `on boot` AND NOT `on property:sys.boot_completed=1`, AND THE DIFFERENCE IS THE WHOLE
        # BUG. A `property:` trigger fires on a CHANGE - init replays it only for an rc it parsed
        # before the property was set. This rc is written when the stage runs, which is AFTER
        # boot, and on the next boot init parses it early enough that `sys.boot_completed` is
        # still 0... and then the property is set and it SHOULD fire. Measured: it did not.
        #
        #     [init.svc.blissos_sshd]: [stopped]
        #     $ setprop ctl.start blissos_sshd
        #     [init.svc.blissos_sshd]: [stopped]        <- still, with no error in dmesg
        #
        # Running the wrapper by hand worked, so the script and every path in it are fine. `on
        # boot` is the trigger init runs for every service file it parsed at startup, and it is
        # the one that does not depend on the ordering of a property it did not observe.
        #
        # THE WAIT MOVED INTO THE WRAPPER, which now polls sys.boot_completed itself before it
        # execs sshd - so the daemon still starts after the framework is up, which is what the
        # property trigger was for.
        "on boot\n"
        "    start blissos_sshd\n"
    )

    for name, body in (("start-sshd.sh", wrapper), ("blissos_sshd.rc", rc)):
        local = os.path.join(work, name)
        with open(local, "w", encoding="utf-8") as fh:
            fh.write(body)
        status, output = adb.run("push", local, f"/data/local/tmp/{name}",
                                 timeout=120, serial=serial)
        if status != 0:
            print(f"  could not push {name}: {output[-200:]}", flush=True)
            return False

    install = (
        "mount -o rw,remount / 2>&1 || true; "
        "cp /data/local/tmp/start-sshd.sh /system/bin/start-sshd.sh; "
        "chmod 755 /system/bin/start-sshd.sh; "
        "cp /data/local/tmp/blissos_sshd.rc /system/etc/init/blissos_sshd.rc; "
        "chmod 644 /system/etc/init/blissos_sshd.rc; "
        "ls -la /system/bin/start-sshd.sh /system/etc/init/blissos_sshd.rc; "
        "echo HOOKED"
    )
    status, output = adb.shell(serial, install, timeout=180)
    print(f"  boot hook -> {status}", flush=True)
    _say(output)
    return status == 0 and "HOOKED" in output


def start(driver, step) -> bool:
    """Write sshd's config, start it, and refuse to continue if it is not listening.

    THE CONFIG IS WRITTEN HERE RATHER THAN SHIPPED, because every line of it is a measured
    response to something:

        HostKey                  Termux's default is /etc/ssh/... which does not exist on Android
        PermitRootLogin yes      this account IS root, and it is the only one the guest has
        PubkeyAuthentication     the only method bionic can check - see the module docstring
        PasswordAuthentication   off, because a password would be a password nobody verifies
        AuthorizedKeysFile       an ABSOLUTE path: the default is relative to the home, and the
                                 home is on an encrypted volume that is not always what sshd thinks
        Subsystem sftp           the payload's own helper

    `UsePAM` IS ABSENT AND WOULD BE FATAL: Termux's sshd is not built with PAM and answers
    "Unsupported option UsePAM".
    """
    serial = _serial(driver)
    work = _work(driver)
    os.makedirs(work, exist_ok=True)

    config = (
        "Port 8022\n"
        f"HostKey {PREFIX}/etc/ssh/ssh_host_rsa_key\n"
        "PermitRootLogin yes\n"
        "PubkeyAuthentication yes\n"
        "PasswordAuthentication no\n"
        "KbdInteractiveAuthentication no\n"
        f"AuthorizedKeysFile {HOME}/.ssh/authorized_keys\n"
        # `StrictModes no` IS REQUIRED ON ANDROID AND ITS ABSENCE WAS THE LAST FAILURE.
        #
        # With everything else correct - the key installed, 700/700/600, root:root, the account in
        # /etc/passwd - the login was still refused, and sshd's own debug log named the reason:
        #
        #     trying public key file /data/local/home/.ssh/authorized_keys
        #     Authentication refused: bad ownership or modes for directory /data
        #
        # `/data` is `drwxrwx--x system system` on Android and ALWAYS WILL BE. StrictModes walks
        # every directory from / down to the key and requires each to be owned by the user or
        # root and not writable by group or other - and /data is owned by `system`, so no path
        # under it can ever satisfy that check.
        #
        # Turning it off is safe HERE and only here, in a way that is worth being explicit about:
        # the directory it stops checking is /data, which is mode 771 and owned by a system uid,
        # so the group that could write to it is `system` - the same trust boundary sshd is
        # already inside. The check adds nothing on this guest, and it is the only thing that
        # prevents a login at all.
        "StrictModes no\n"
        # `LogLevel DEBUG1` IS FOR THE FAILURE THIS STAGE KEEPS HITTING. With the default, sshd
        # writes "Server listening" and nothing about the authentication that follows - so five
        # CI runs reported `Permission denied (publickey)` with the server silent about why. The
        # line that names the reason lives at DEBUG1 and only in `sshd-session`'s log:
        #
        #     trying public key file /data/local/home/.ssh/authorized_keys
        #     Authentication refused: bad ownership or modes for directory /data
        #
        # It costs a few lines per connection on a disk nobody reads logs from.
        "LogLevel DEBUG1\n"
        f"PidFile /data/local/tmp/sshd.pid\n"
        f"Subsystem sftp {PREFIX}/libexec/sftp-server\n"
    )
    local = os.path.join(work, "sshd_config")
    with open(local, "w", encoding="utf-8") as fh:
        fh.write(config)
    status, output = adb.run("push", local, SSHD_CONFIG, timeout=120, serial=serial)
    if status != 0:
        print(f"  could not push the config: {output[-300:]}", flush=True)
        return False

    # THE DIRECTORY HAS TO EXIST BEFORE THE PUSH, AND `adb push` DOES NOT SAY WHEN IT DOES NOT.
    #
    # This is the bug that cost the most runs, and it was invisible because `adb push` into a
    # missing directory CREATES A ZERO-BYTE FILE and answers 0:
    #
    #     $ ls -la /data/local/sshd/etc/ssh/
    #     -rw------- 1 root root      0 ... ssh_host_rsa_key
    #     -rw-rw-rw- 1 root root      0 ... sshd_config.installed
    #
    # So the persistent config the boot wrapper reads was EMPTY, sshd fell back to every default
    # (`StrictModes yes`, `AuthorizedKeysFile .ssh/authorized_keys`), and every login was refused
    # while `sshd -T` - run against the TEMPFS copy, which was written correctly - showed a config
    # that was right. Two files, one written before its directory existed, and six CI runs.
    mkdir = (f"mkdir -p {PREFIX}/etc/ssh; "
             f"chmod 755 {PREFIX}/etc/ssh; "
             f"ls -ld {PREFIX}/etc/ssh")
    status, output = adb.shell(serial, mkdir, timeout=60)
    if status != 0:
        print(f"  could not create {PREFIX}/etc/ssh: {output[-300:]}", flush=True)
        return False

    # AND A COPY THE BOOT WRAPPER CAN RESTORE, because /data/local/tmp is a tmpfs and the
    # artifact is rebooted on a machine this stage never sees.
    status, output = adb.run("push", local, f"{PREFIX}/etc/ssh/sshd_config.installed",
                             timeout=120, serial=serial)
    if status != 0:
        print(f"  could not push the persistent config: {output[-300:]}", flush=True)
        return False
    # A ZERO-BYTE FILE IS THE FAILURE THIS CHECK EXISTS FOR, so it is measured rather than
    # assumed: `adb push` answering 0 means "the transfer was attempted".
    status, size = adb.shell(
        serial, f"wc -c < {PREFIX}/etc/ssh/sshd_config.installed", timeout=60)
    if status != 0 or not size.strip().isdigit() or int(size.strip()) < 50:
        print(f"  the persistent config is {size.strip() or 'missing'} bytes; "
              f"the boot wrapper would start sshd with defaults", flush=True)
        return False

    # KEYS, THE PRIVILEGE SEPARATION DIRECTORY, THEN THE DAEMON. `-E` sends its log to a file this
    # step reads back, because a daemon that fails to start prints to nowhere anybody is watching.
    start_command = (
        f"P={PREFIX}; export LD_LIBRARY_PATH=$P/lib; "
        f"mkdir -p $P/etc/ssh $P/var/empty $P/var/run; "
        f"chmod 755 $P/var/empty; "
        # AND THE PRIVILEGE SEPARATION DIRECTORY AT THE PATH sshd WAS COMPILED WITH.
        #
        # This is the one part of the prefix this stage cannot move. sshd computes its privsep
        # chroot from a path baked in at build time, and Termux's is
        #
        #     /data/data/com.termux/files/usr/var/empty
        #
        # no matter what `-f` says - so a payload installed at /data/local/sshd started with
        #
        #     Missing privilege separation directory: /data/data/com.termux/files/usr/var/empty
        #
        # and exited. That PATH CAN BE CREATED even though its parent cannot be COPIED INTO:
        # mode 700 and `app_data_file` stop a file copy, and `mkdir` as root succeeds - measured.
        # So the directory is made, empty, which is all sshd wants it for.
        f"mkdir -p /data/data/com.termux/files/usr/var/empty 2>/dev/null; "
        f"chmod 755 /data/data/com.termux/files/usr/var/empty 2>/dev/null; "
        # AND THE HELPER sshd EXECS FOR EACH CONNECTION, AT THE SAME COMPILED-IN PATH.
        #
        # sshd 10.6 splits the per-connection work into `libexec/sshd-session`, and that path is
        # baked in beside the privsep one - so moving the payload to /data/local/sshd made the
        # daemon bind the port and then refuse every connection with
        #
        #     /data/data/com.termux/files/usr/libexec/sshd-session
        #     does not exist or is not executable
        #
        # THE SAME MKDIR-AND-COPY TRICK WORKS HERE, and it is worth being precise about why the
        # whole tree cannot simply be installed at the compiled-in prefix: `cp -a` of a DIRECTORY
        # into an `app_data_file` parent is refused, while `mkdir` and a per-FILE copy succeed -
        # measured. So the two files sshd needs at that path are placed there, one at a time, and
        # everything else stays where root can write freely.
        f"T=/data/data/com.termux/files/usr; "
        f"mkdir -p $T/libexec $T/bin 2>/dev/null; "
        # AND A `bash` AT THE PATH sshd DEMANDS, WHICH IS NOT THE ONE IN /etc/passwd.
        #
        # This is the last thing standing between the key and a shell, and it says so plainly:
        #
        #     User root not allowed because shell /data/data/com.termux/files/usr/bin/bash
        #     does not exist
        #
        # /etc/passwd says `/data/local/sshd/bin/bash` - this stage writes it there - and sshd
        # still names the Termux path, because the DEFAULT login shell is compiled in and used
        # when the account is resolved before the passwd entry is honoured. Putting the same
        # binary at the compiled-in path satisfies both readers at once.
        # A WRAPPER, NOT A COPY, BECAUSE bash NEEDS ITS LIBRARIES AND THE COMPILED-IN PREFIX IS
        # NOT WHERE THEY ARE. Copying the binary produced the next failure in the chain:
        #
        #     CANNOT LINK EXECUTABLE "bash": library "libandroid-support.so" not found
        #     needed by main executable
        #
        # and the same would be true of every library it needs. A three-line script that sets
        # `LD_LIBRARY_PATH` and execs the real one satisfies sshd's "this path is executable"
        # check and gives bash what it needs to link.
        f"printf '%s\\n' '#!/system/bin/sh' "
        f"'export LD_LIBRARY_PATH={PREFIX}/lib' "
        f"'exec {PREFIX}/bin/bash \"$@\"' > $T/bin/bash 2>/dev/null; "
        f"chmod 755 $T/bin/bash 2>/dev/null; "
        # AND THE HOME sshd DEFAULTS TO, so the login does not start with a chdir failure:
        #
        #     Could not chdir to home directory /data/data/com.termux/files/home:
        #     No such file or directory
        f"mkdir -p /data/data/com.termux/files/home 2>/dev/null; "
        f"chmod 700 /data/data/com.termux/files/home 2>/dev/null; "
        # THE WHOLE libexec DIRECTORY, NOT THE ONE HELPER THAT COMPLAINED FIRST.
        #
        # `sshd-session` was the first name to appear in an error and copying it exposed the
        # next one - `sshd-auth` - and there are five helpers in that directory. Copying them one
        # per CI run is a loop that costs twenty minutes an iteration, and the SET is what sshd
        # 10.6 actually needs at its compiled-in prefix:
        #
        #     sshd-session  sshd-auth  ssh-keysign  ssh-pkcs11-helper  ssh-sk-helper
        #
        # So every regular file in `libexec` is placed there. The directory cannot be `cp -a`'d,
        # but individual files can, and this is the last place the compiled-in prefix matters.
        f"for f in $P/libexec/*; do "
        f"[ -f \"$f\" ] && cp \"$f\" $T/libexec/ 2>/dev/null; done; "
        f"chmod 755 $T/libexec/* 2>/dev/null; "
        # `-s` AND NOT `-f`: A ZERO-BYTE KEY PASSES `[ -f ]` AND IS THE FORMAT sshd REFUSES.
        #
        #     Unable to load host key ".../ssh_host_rsa_key": invalid format
        #     sshd: no hostkeys available -- exiting.
        #
        # which is what an `adb push` into a directory that did not exist leaves behind.
        f"[ -s $P/etc/ssh/ssh_host_rsa_key ] || "
        f"{{ rm -f $P/etc/ssh/ssh_host_rsa_key $P/etc/ssh/ssh_host_rsa_key.pub; "
        f"$P/bin/ssh-keygen -t rsa -b 2048 -f $P/etc/ssh/ssh_host_rsa_key -N '' -q; }}; "
        f"test -s $P/etc/ssh/ssh_host_rsa_key || {{ echo NO_HOST_KEY; exit 1; }}; "
        f"[ -f /data/local/tmp/sshd.pid ] && kill \"$(cat /data/local/tmp/sshd.pid)\" 2>/dev/null; "
        # `-e` AND A REDIRECT, NOT `-E`, BECAUSE THE AUTHENTICATION HAPPENS IN A CHILD.
        #
        # `-E file` sends the PARENT's log to that file. sshd 10.6 forks `sshd-session` for each
        # connection, and that child writes its own - so five CI runs had a log containing
        # "Server listening" and not one line about the login that was refused a second later:
        #
        #     --- sshd.err ---
        #     debug1: Bind to port 8022 on ::.
        #     Server listening on :: port 8022.
        #
        # `-e` writes to stderr, and the shell redirects BOTH the parent and every child into the
        # same file, which is the only way the reason reaches the log this stage reads.
        # `setsid ... &` AND NOT A BARE `&`, BECAUSE `adb shell` KILLS ITS CHILDREN ON EXIT.
        #
        # `adb shell "cmd &"` returns as soon as the shell exits, and the guest's adbd then sends
        # SIGHUP to the process group - so a daemon started that way is gone before the next adb
        # command arrives. `setsid` puts it in its own session with no controlling terminal, which
        # is what survives; the `&` then lets the same shell go on to check the socket.
        #
        # AND `-e` GOES TO A FILE THIS SHELL OWNS. `-e` writes to stderr, so the redirect has to be
        # on the sshd process itself; wrapping it in `sh -c '... 2>&1'` gives the parent AND the
        # per-connection children one destination, which `-E file` could not do.
        # `$P` IS EXPANDED HERE, NOT INSIDE THE INNER SHELL. A single-quoted `sh -c '...$P...'`
        # passes the dollar through to a shell that has no `P` in it - measured:
        #
        #     sh: /bin/sshd: inaccessible or not found
        #
        # because `$P` had become the empty string and the path collapsed to `/bin/sshd`. The
        # prefix is known now, so it is written out.
        f"setsid sh -c '{PREFIX}/bin/sshd -D -e -f {SSHD_CONFIG}' "
        f">> /data/local/tmp/sshd.err 2>&1 & "
        f"sleep 3; "
        f"tail -5 /data/local/tmp/sshd.err 2>/dev/null; "
        f"grep -q ':1F56' /proc/net/tcp && echo SSHD_LISTENING"
    )
    status, output = adb.shell(serial, start_command, timeout=300)
    print(f"  start -> {status}", flush=True)
    _say(output)
    # THE VERDICT IS THE LISTENING SOCKET, NOT THE EXIT STATUS: `sshd` forks and answers 0 even
    # when the child it forked died, which is exactly how the missing-library failures presented.
    if "SSHD_LISTENING" in output:
        # THE DAEMON IS UP. ITS LOG IS KEPT AND PRINTED ANYWAY, because the step that follows is
        # the one that authenticates and the reason a key is refused lives in this file, not in
        # the ssh client's output:
        #
        #     trying public key file /data/local/home/.ssh/authorized_keys
        #     Authentication refused: bad ownership or modes for directory /data
        #
        # Six runs were spent looking at the client side of that. Printing the server's own log
        # the moment the daemon starts makes the next failure name itself.
        status, tail = adb.shell(serial, "tail -20 /data/local/tmp/sshd.err 2>/dev/null",
                                 timeout=60)
        if tail.strip():
            print("  sshd's own log:", flush=True)
            _say(tail, limit=20)
        # AND WHAT sshd THINKS ITS CONFIGURATION IS. `-T` prints the EFFECTIVE values after
        # include processing, defaults and overrides - so a line that did not take effect says so
        # here, instead of only in the reason a login failed:
        #
        #     $ sshd -T | grep -E 'strictmodes|authorizedkeysfile|permitrootlogin'
        status, effective = adb.shell(
            serial,
            f"P={PREFIX}; export LD_LIBRARY_PATH=$P/lib; "
            f"$P/bin/sshd -T -f {SSHD_CONFIG} 2>&1 | "
            f"grep -iE '^(strictmodes|authorizedkeysfile|permitrootlogin|pubkeyauth|"
            f"passwordauth|usepam|loglevel) '",
            timeout=60)
        if effective.strip():
            print("  sshd's effective config:", flush=True)
            _say(effective, limit=20)
        return True
    print("  sshd did not reach a listening socket; its log is above", flush=True)
    return False
