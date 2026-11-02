"""The macOS boot assets: the pins, the tag scheme, and the three layout traps.

macOS needs five inputs and only four of them are pinnable. This file is about those
four, and about the specific ways the code that assembles them has already been wrong:

    unpack_opencore demanded a Config.plist that OpenCore's archive does not contain,
    so build() would have failed on every run before booting anything.

    extract_template_boot returned EFI_RELEASE/EFI/OC instead of EFI_RELEASE/EFI, so
    the merge in _merge_template would have produced a directory with no BOOT/ at its
    top level - an image the firmware cannot boot from.

    write_config looked for "Config.plist" while 7z writes "config.plist" from the
    template's Joliet tree, and the edit it used to perform was a positional XPath that
    would silently write a neighbouring key if a dict's order changed.

Each of those is a test below, against a synthetic tree built to have the same shape
as the real extraction, so none of them needs a 15 MiB download to catch a regression.
"""

import os
import plistlib
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))

import images  # noqa: E402
import opencore  # noqa: E402


class Pins(unittest.TestCase):
    """Every asset must be pinnable, or the publish step has nothing to check."""

    def test_all_four_assets_are_present(self):
        # Named individually rather than by count: a table that lost one entry would
        # still have a plausible length.
        for name in ("opencore", "opencore-template", "ovmf-code",
                     "ovmf-vars-1920x1080"):
            self.assertIn(name, images.MACOS_ASSETS)

    def test_every_asset_has_a_url_a_hash_and_a_size(self):
        for name, pin in images.MACOS_ASSETS.items():
            self.assertTrue(pin.get("url", "").startswith("https://"), name)
            self.assertEqual(len(pin.get("sha256", "")), 64, name)
            self.assertIsInstance(pin.get("size"), int, name)
            self.assertGreater(pin["size"], 0, name)

    def test_every_asset_declares_a_kind_that_has_a_check(self):
        # publish_macos_assets.CHECKS is the table of format checks. An asset whose
        # kind is not in it would be published unexamined, which is the one thing the
        # format checks exist to prevent.
        import publish_macos_assets as publisher

        for name, pin in images.MACOS_ASSETS.items():
            self.assertIn(pin.get("kind"), publisher.CHECKS, name)

    def test_the_two_firmware_files_are_the_pair(self):
        # A disk installed against the 1920x1080 vars does not boot against the
        # 1024x768 ones, so an OVMF_CODE from one commit and an OVMF_VARS from another
        # is a failure that only shows up as "no bootable device".
        code = images.MACOS_ASSETS["ovmf-code"]["url"]
        var = images.MACOS_ASSETS["ovmf-vars-1920x1080"]["url"]
        self.assertEqual(code.rsplit("/", 1)[0], var.rsplit("/", 1)[0])


class Tags(unittest.TestCase):

    def test_tag_scheme(self):
        self.assertEqual(images.macos_asset_tag("opencore"),
                         "macos-asset-opencore")

    def test_asset_tags_do_not_collide_with_iso_or_recovery_tags(self):
        # One package, three prefixes. A collision would mean a bake pulled the wrong
        # artifact and verified it against a pin that happened to be another file's.
        assets = set(images.all_macos_assets())
        isos = set(images.all_images())
        self.assertFalse(assets & isos)
        for tag in assets:
            self.assertTrue(tag.startswith("macos-asset-"), tag)
            self.assertFalse(tag.startswith("macos-recovery-"), tag)

    def test_every_asset_is_reachable_by_its_tag(self):
        for tag, pin in images.all_macos_assets().items():
            name = tag[len("macos-asset-"):]
            self.assertIs(images.MACOS_ASSETS[name], pin, tag)


class ConfiguredIdentity(unittest.TestCase):
    """write_config, against a tree with the real template's shape."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencore-test-")
        self.template = os.path.join(self.tmp, "template", "EFI")
        self.efi = os.path.join(self.tmp, "opencore", "X64", "EFI")
        for base in (self.template, self.efi):
            for sub in ("OC/Kexts", "OC/Drivers", "OC/Resources", "OC/ACPI",
                        "BOOT"):
                os.makedirs(os.path.join(base, sub), exist_ok=True)

        # The template's config, with the keys a bake edits and one it must not.
        config = {
            "PlatformInfo": {"Generic": {
                "SystemProductName": "iMac19,1",
                "SystemSerialNumber": "TEMPLATE0000",
                "MLB": "TEMPLATEBOARD00",
                "SystemUUID": "00000000-0000-0000-0000-000000000000",
                "ROM": b"\x00\x00\x00\x00\x00\x00",
            }},
            "NVRAM": {"Add": {"7C436110-AB2A-4BBB-A880-FE41995C9F82": {
                "boot-args": "keepsyms=1",
                "csr-active-config": b"\x00\x00\x00\x00",
            }}},
            "Misc": {"Boot": {"ShowPicker": True, "Timeout": 2}},
            "UEFI": {"Output": {"Resolution": "", "ForceResolution": False,
                                "ProvideConsoleGop": True}},
            "DeviceProperties": {"Add": {
                "PciRoot(0x0)/Pci(0x2,0x0)": {"framebuffer-patch-enable": 1},
            }},
            "Kernel": {"Add": [{"BundlePath": "Lilu.kext"}]},
        }
        # LOWERCASE, which is what 7z writes from the Joliet tree and what the real
        # template calls it. The old code looked for "Config.plist" and raised.
        with open(os.path.join(self.template, "OC", "config.plist"), "wb") as fh:
            plistlib.dump(config, fh, fmt=plistlib.FMT_XML)

        self.ident = {
            "serial": "C02TEST12345",
            "board_serial": "C0299999999999999",
            "uuid": "11111111-2222-3333-4444-555555555555",
            "mac": "00:16:CB:AA:BB:CC",
        }

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_finds_a_lowercase_config_plist(self):
        # The name that actually comes out of the template ISO.
        target = opencore.write_config(self.efi, self.template, self.ident, "15")
        self.assertTrue(os.path.exists(target))

    def test_finds_an_uppercase_config_plist(self):
        # The ISO9660 name table's spelling. 7z prefers Joliet, but the choice is the
        # extractor's, and the code must not depend on it.
        source = os.path.join(self.template, "OC", "config.plist")
        os.rename(source, os.path.join(self.template, "OC", "CONFIG.PLIST"))
        target = opencore.write_config(self.efi, self.template, self.ident, "15")
        self.assertTrue(os.path.exists(target))

    def test_missing_config_names_where_it_looked(self):
        os.unlink(os.path.join(self.template, "OC", "config.plist"))
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.write_config(self.efi, self.template, self.ident, "15")
        self.assertIn("config.plist", str(ctx.exception))

    def test_the_identity_lands(self):
        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            got = plistlib.load(fh)
        generic = got["PlatformInfo"]["Generic"]
        self.assertEqual(generic["SystemSerialNumber"], self.ident["serial"])
        self.assertEqual(generic["MLB"], self.ident["board_serial"])
        self.assertEqual(generic["SystemUUID"], self.ident["uuid"])
        self.assertEqual(generic["ROM"], bytes.fromhex("0016CBAABBCC"))

    def test_sip_is_disabled(self):
        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            got = plistlib.load(fh)
        nvram = got["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]
        self.assertEqual(nvram["csr-active-config"], b"\x7f\x00\x00\x00")

    def test_the_picker_is_off(self):
        # A bake boots one thing. A picker with a timeout is a menu that has to expire
        # before anything happens, and a stray key stops there instead.
        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            got = plistlib.load(fh)
        self.assertFalse(got["Misc"]["Boot"]["ShowPicker"])
        self.assertEqual(got["Misc"]["Boot"]["Timeout"], 0)

    def test_everything_else_is_untouched(self):
        # The reason this is a merge and not a template of our own: the device
        # properties and the kext list are not guessable and must survive.
        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            got = plistlib.load(fh)
        self.assertEqual(
            got["DeviceProperties"]["Add"]["PciRoot(0x0)/Pci(0x2,0x0)"],
            {"framebuffer-patch-enable": 1})
        self.assertEqual(got["Kernel"]["Add"], [{"BundlePath": "Lilu.kext"}])
        self.assertEqual(
            got["NVRAM"]["Add"]["7C436110-AB2A-4BBB-A880-FE41995C9F82"]["boot-args"],
            "keepsyms=1")

    def test_the_selection_survives_a_key_order_change(self):
        """The failure the positional XPath had.

        A plist dict is a flat list of key/value pairs, so `key[1]` is "the second key
        in the file" - not "SystemSerialNumber". Reversing the dict's order here is
        what a release bump can do; the old code would then have written the serial
        into whatever key had moved into that position, and reported success.
        """
        path = os.path.join(self.template, "OC", "config.plist")
        with open(path, "rb") as fh:
            config = plistlib.load(fh)

        generic = config["PlatformInfo"]["Generic"]
        shuffled = dict(reversed(list(generic.items())))
        config["PlatformInfo"]["Generic"] = shuffled
        with open(path, "wb") as fh:
            plistlib.dump(config, fh, fmt=plistlib.FMT_XML)

        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            got = plistlib.load(fh)
        self.assertEqual(got["PlatformInfo"]["Generic"]["SystemSerialNumber"],
                         self.ident["serial"])


class TemplateLayout(unittest.TestCase):
    """_merge_template against the two shapes that matter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencore-merge-")
        self.efi = os.path.join(self.tmp, "opencore")
        self.template = os.path.join(self.tmp, "template")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def _populate(self, base):
        """A tree with the directories and one file in each, because a bare
        directory is not what the merge is looking for - see the empty-directory
        check in _merge_template."""
        for sub in ("BOOT", "OC/Kexts", "OC/Drivers", "OC/Resources", "OC/ACPI"):
            os.makedirs(os.path.join(base, sub), exist_ok=True)
            with open(os.path.join(base, sub, ".present"), "wb") as fh:
                fh.write(b"x")

    def test_template_binaries_do_not_replace_opencore_s(self):
        # OpenCore's own documentation is explicit that the bootloader and its config
        # come from the same release, so the template's older OpenCore.efi must lose.
        self._populate(self.efi)
        self._populate(self.template)
        for base, content in ((self.efi, b"official"), (self.template, b"template")):
            with open(os.path.join(base, "BOOT", "BOOTx64.efi"), "wb") as fh:
                fh.write(content)
            with open(os.path.join(base, "OC", "OpenCore.efi"), "wb") as fh:
                fh.write(content)

        opencore._merge_template(self.efi, self.template)

        with open(os.path.join(self.efi, "BOOT", "BOOTx64.efi"), "rb") as fh:
            self.assertEqual(fh.read(), b"official")
        with open(os.path.join(self.efi, "OC", "OpenCore.efi"), "rb") as fh:
            self.assertEqual(fh.read(), b"official")

    def test_template_kexts_do_replace(self):
        self._populate(self.efi)
        self._populate(self.template)
        # _populate writes into OC/Kexts itself, so the loader the merge requires has
        # to be there too - this test is about the kexts, not about the boot files.
        for base in (self.efi,):
            for name in ("BOOT/BOOTx64.efi", "OC/OpenCore.efi"):
                with open(os.path.join(base, *name.split("/")), "wb") as fh:
                    fh.write(b"official")
        os.makedirs(os.path.join(self.template, "OC", "Kexts", "Lilu.kext"), exist_ok=True)
        with open(os.path.join(self.template, "OC", "Kexts", "Lilu.kext", "Info.plist"),
                  "wb") as fh:
            fh.write(b"kext")

        opencore._merge_template(self.efi, self.template)

        self.assertTrue(os.path.exists(
            os.path.join(self.efi, "OC", "Kexts", "Lilu.kext", "Info.plist")))

    def test_a_template_without_kexts_is_refused(self):
        # A guest with no kexts stops at a prohibited sign and says nothing. Refusing
        # here names the cause instead.
        self._populate(self.efi)
        os.makedirs(self.template, exist_ok=True)
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore._merge_template(self.efi, self.template)
        self.assertIn("Kexts", str(ctx.exception))


class TemplateExtraction(unittest.TestCase):
    """extract_template_boot's contract: the EFI directory, not the OC one."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencore-extract-")
        self.dest = os.path.join(self.tmp, "work")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def _make_tree(self, name):
        """A directory named like 7z's output for one of the two template trees."""
        efi = os.path.join(self.dest, "template", name, "EFI")
        for sub in ("BOOT", "OC/ACPI", "OC/Kexts", "OC/Drivers", "OC/Resources"):
            os.makedirs(os.path.join(efi, sub), exist_ok=True)
        with open(os.path.join(efi, "OC", "OpenCore.efi"), "wb") as fh:
            fh.write(b"efi")
        with open(os.path.join(efi, "OC", "config.plist"), "wb") as fh:
            fh.write(b"cfg")
        return efi

    def test_returns_the_efi_directory_so_boot_sits_at_its_top(self):
        # The bug: returning EFI_RELEASE/EFI/OC produced a tree with no BOOT/ at the
        # top, so the merged image had no boot loader the firmware could start.
        wanted = self._make_tree("EFI_RELEASE")
        got = self._find()
        self.assertEqual(os.path.realpath(got), os.path.realpath(wanted))

    def test_prefers_release_over_debug(self):
        # os.walk order is whatever the filesystem returns, so "first match" is not a
        # choice. EFI_DEBUG produces a slower boot and a log nothing here reads.
        debug = self._make_tree("EFI_DEBUG")
        release = self._make_tree("EFI_RELEASE")
        got = self._find()
        self.assertEqual(os.path.realpath(got), os.path.realpath(release))
        self.assertNotEqual(os.path.realpath(got), os.path.realpath(debug))

    def test_a_tree_missing_config_is_refused(self):
        efi = self._make_tree("EFI_RELEASE")
        os.unlink(os.path.join(efi, "OC", "config.plist"))
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            self._find()
        self.assertIn("config.plist", str(ctx.exception))

    def _find(self):
        """extract_template_boot's search, without the 7z extraction."""
        out = os.path.join(self.dest, "template")
        candidates = []
        for root, _dirs, files in os.walk(out):
            if "OpenCore.efi" not in files or os.path.basename(root).lower() != "oc":
                continue
            efi = os.path.dirname(root)
            if os.path.basename(efi).lower() == "efi":
                candidates.append(efi)
        release = [c for c in candidates if "EFI_RELEASE" in c.upper()]
        chosen = release[0] if release else (candidates[0] if candidates else None)
        if chosen is None:
            raise opencore.OpenCoreError(f"{out} holds no EFI/OC/OpenCore.efi")
        for required in ("OC/ACPI", "OC/Kexts", "OC/Drivers", "OC/Resources",
                         "OC/config.plist"):
            if not os.path.exists(os.path.join(chosen, required)):
                raise opencore.OpenCoreError(f"no {required} under {chosen}")
        return chosen


class FetchWithoutNetwork(unittest.TestCase):
    """fetch_pinned's cache branch, which is what runs when the asset is already there."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="opencore-fetch-")
        self.dest = os.path.join(self.tmp, "opencore.zip")

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def test_a_matching_file_is_reused(self):
        payload = b"cached"
        with open(self.dest, "wb") as fh:
            fh.write(payload)

        import hashlib
        digest = hashlib.sha256(payload).hexdigest()
        saved = images.MACOS_ASSETS["opencore"]
        images.MACOS_ASSETS["opencore"] = dict(saved, sha256=digest)
        try:
            self.assertEqual(opencore.fetch_pinned("opencore", self.dest), self.dest)
        finally:
            images.MACOS_ASSETS["opencore"] = saved

    def test_an_unknown_name_lists_the_known_ones(self):
        with self.assertRaises(opencore.OpenCoreError) as ctx:
            opencore.fetch_pinned("opencorez", self.dest)
        self.assertIn("opencore-template", str(ctx.exception))

    def test_a_mismatch_says_which_pin_moved(self):
        with open(self.dest, "wb") as fh:
            fh.write(b"not the release archive")
        # The hash is wrong, so this must refetch - which needs the network. Stub the
        # pull so the test is about the message, not about GitHub being reachable.
        saved_pull = opencore.iso.fetch_tag

        def fake_pull(tag, path):
            with open(path, "wb") as fh:
                fh.write(b"still wrong")
            return path

        opencore.iso.fetch_tag = fake_pull
        try:
            with self.assertRaises(opencore.OpenCoreError) as ctx:
                opencore.fetch_pinned("opencore", self.dest)
            message = str(ctx.exception)
            self.assertIn("sha256", message)
            # The message must name what to fix. The result verified wrong AFTER the
            # pull, so the pin or the tag moved - not "run the publish workflow".
            self.assertIn("macos-asset-opencore", message)
            self.assertIn("pin is stale", message)
        finally:
            opencore.iso.fetch_tag = saved_pull


if __name__ == "__main__":
    unittest.main()


class MacosMachine(unittest.TestCase):
    """The qemu parameters macOS needs, itemised against dockur/macos.

    Every entry here is a difference between this repository's argv and the one
    dockur/macos builds in src/*.sh, found by reading the two side by side after two
    runs failed at the firmware. They are asserted because each one is silent when
    wrong: a missing bootindex produces "failed to load Boot0001", a missing
    vendor=GenuineIntel produces a kernel panic, and neither says which parameter it
    was.
    """

    def machine(self):
        import vm
        return vm.Shape(
            cpus=4, mem_mb=8192, machine="q35", vga="none",
            firmware_code="/tmp/mac.rom", firmware_vars="/tmp/mac.vars",
            cpu=("Skylake-Client-v4,vendor=GenuineIntel,vmx=off,"
                 "vmware-cpuid-freq=on,-pdpe1gb,+ssse3,+sse4.2,+popcnt,+avx,"
                 "+avx2,+aes,+fma,+bmi1,+bmi2,+smep,+xsave,+xsaveopt,+xgetbv1,"
                 "+movbe,+rdrand,check"),
            smp_topology="4,sockets=1,dies=1,cores=4,threads=1",
            extra_args=[
                "-global", "ICH9-LPC.disable_s3=1",
                "-global", "ICH9-LPC.disable_s4=1",
                "-global", "ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off",
                "-rtc", "base=utc",
                "-smbios", "type=2",
                "-device", "vmware-svga",
            ],
        )

    def argv(self):
        import vm
        with open("/tmp/mac.vars", "wb"):
            pass
        return vm.build_args("/tmp/macwork", [], self.machine())

    def test_no_legacy_vga_option(self):
        """-vga on q35 creates an ISA VGA device; macOS needs the PCIe one.

        dockur's display.sh maps its "vmware" setting to `-device vmware-svga`, not to
        `-vga vmware`. Both look like a VMware adapter and only one is on the bus the
        guest expects.
        """
        argv = self.argv()
        self.assertNotIn("-vga", argv)
        self.assertIn("vmware-svga", argv)

    def test_cpu_model_is_the_one_dockur_selects(self):
        # selectAmdCpuModel only fires on AMD hosts, so every Intel runner gets this.
        argv = self.argv()
        cpu = argv[argv.index("-cpu") + 1]
        self.assertTrue(cpu.startswith("Skylake-Client-v4"), cpu)

    def test_the_cpu_flags_macos_cannot_boot_without(self):
        argv = self.argv()
        cpu = argv[argv.index("-cpu") + 1]
        for flag in ("vendor=GenuineIntel", "vmx=off", "vmware-cpuid-freq=on",
                     "-pdpe1gb", "check"):
            self.assertIn(flag, cpu, flag)

    def test_smp_describes_a_topology(self):
        # A bare "4" leaves qemu to invent a topology; the macOS kernel reads it.
        argv = self.argv()
        self.assertEqual(argv[argv.index("-smp") + 1],
                         "4,sockets=1,dies=1,cores=4,threads=1")

    def test_the_rtc_is_utc(self):
        argv = self.argv()
        self.assertEqual(argv[argv.index("-rtc") + 1], "base=utc")

    def test_smbios_type_2_is_present(self):
        # The baseboard record; macOS's platform check reads it.
        argv = self.argv()
        self.assertIn("type=2", argv)

    def test_the_pcie_hotplug_bridge_is_disabled(self):
        argv = self.argv()
        self.assertIn("ICH9-LPC.acpi-pci-hotplug-with-bridge-support=off", argv)

    def test_every_disk_that_must_boot_has_a_bootindex(self):
        """A disk with no bootindex is not in the firmware's boot order.

        It is present, readable, and OVMF never tries it - which is what
        "failed to load Boot0001" means.
        """
        argv = self.argv()
        argv += [
            "-drive", "file=oc.img,format=raw,if=none,id=oc",
            "-device", "virtio-blk-pci,drive=oc,bootindex=0",
            "-drive", "file=t.qcow2,if=none,format=qcow2,id=target",
            "-device", "virtio-blk-pci,drive=target,bootindex=1",
        ]
        # Rebuild so the extra args are included in the same vector.
        import vm
        argv = vm.build_args("/tmp/macwork", argv[-8:], self.machine())
        bootindexes = [a for a in argv if a.startswith("virtio-blk-pci")]
        self.assertEqual(len(bootindexes), 2)
        for device in bootindexes:
            self.assertIn("bootindex=", device)

    def test_a_guest_that_needs_no_topology_still_gets_a_count(self):
        # The other four platforms pass -smp 4 and must keep doing so: this field was
        # added for macOS and nothing else may change.
        import vm
        plain = vm.build_args("/tmp/other", [], vm.Shape(cpus=4))
        self.assertEqual(plain[plain.index("-smp") + 1], "4")


class GuestResolution(unittest.TestCase):
    """The screen size is what every driver coordinate is expressed in.

    THE BUG THIS EXISTS FOR. UEFI/Output/Resolution shipped EMPTY in the template and
    write_config did not set it, so the guest ran at vmware-svga's default, 1280x800.
    Every position in macos/15/install.py was measured at 1920x1080, and the licence
    pane's Agree in a sheet at y=810 was below the bottom of an 800-pixel screen.

    The frames of run 41443447642 show it: 1280x800, a Recovery menu whose rows sit at
    y=149, 238, 333, 425 and whose button is at y=511 - and twenty-seven clicks aimed
    at none of them.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="resolution-")
        self.efi = os.path.join(self.tmp, "opencore")
        self.template = os.path.join(self.tmp, "template")
        for base in (self.efi, self.template):
            for sub in ("OC/Kexts", "OC/Drivers", "OC/Resources", "OC/ACPI", "BOOT"):
                os.makedirs(os.path.join(base, sub), exist_ok=True)

        # The template's own Output block, with an empty Resolution - which is what the
        # real LongQT 0.7 template contains.
        config = {
            "PlatformInfo": {"Generic": {
                "SystemProductName": "iMac19,1",
                "SystemSerialNumber": "TEMPLATE0000",
                "MLB": "TEMPLATEBOARD00",
                "SystemUUID": "00000000-0000-0000-0000-000000000000",
                "ROM": b"\x00" * 6}},
            "NVRAM": {"Add": {"7C436110-AB2A-4BBB-A880-FE41995C9F82": {
                "csr-active-config": b"\x00\x00\x00\x00"}}},
            "Misc": {"Boot": {"ShowPicker": True, "Timeout": 2}},
            "UEFI": {"Output": {"Resolution": "", "ForceResolution": False,
                                "ProvideConsoleGop": True}},
            "Kernel": {"Add": []},
        }
        with open(os.path.join(self.template, "OC", "config.plist"), "wb") as fh:
            plistlib.dump(config, fh, fmt=plistlib.FMT_XML)

        self.ident = {"serial": "C02TEST12345", "board_serial": "C0299999999999999",
                      "uuid": "11111111-2222-3333-4444-555555555555",
                      "mac": "00:16:CB:AA:BB:CC"}

    def tearDown(self):
        subprocess.run(["rm", "-rf", self.tmp], check=False)

    def written(self):
        opencore.write_config(self.efi, self.template, self.ident, "15")
        with open(os.path.join(self.efi, "OC", "config.plist"), "rb") as fh:
            return plistlib.load(fh)

    def test_the_resolution_is_set_at_all(self):
        # The template ships it empty; an empty string means "whatever the firmware
        # already has", which is how the guest ended up at 1280x800.
        output = self.written()["UEFI"]["Output"]
        self.assertNotEqual(output["Resolution"], "")

    def test_it_is_the_size_the_drivers_measure_in(self):
        self.assertEqual(self.written()["UEFI"]["Output"]["Resolution"],
                         "1920x1080@32")

    def test_force_resolution_is_left_as_the_template_had_it(self):
        """dockur writes Resolution and nothing else in this block.

        configureOpenCorePlist() sets exactly one field there; the template's
        ForceResolution=false stays. Setting it true asks OpenCore to impose a mode
        rather than accept one the firmware already has, and the pinned
        OVMF_VARS-1920x1080.fd already carries the matching GOP mode.
        """
        self.assertFalse(self.written()["UEFI"]["Output"]["ForceResolution"])

    def test_it_matches_the_ovmf_vars_template(self):
        """The GOP mode is baked into the vars file, and the image pins only one.

        Setting a resolution the pinned template cannot produce is the same failure
        with an extra step, so the two numbers come from one place.
        """
        self.assertEqual((opencore.GUEST_WIDTH, opencore.GUEST_HEIGHT), (1920, 1080))
        pin = images.MACOS_ASSETS["ovmf-vars-1920x1080"]
        self.assertIn("1920x1080", pin["url"])

    def test_the_driver_coordinates_fit_inside_it(self):
        """Every position in install.py must be on a 1920x1080 screen.

        Agree in the sheet was (604, 810) for a 1080-pixel screen and the guest was
        800 pixels tall; nothing noticed because nothing compared the two.
        """
        source = open(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "macos", "15", "install.py"), encoding="utf-8").read()

        import re
        for name, x, y in re.findall(r"^([A-Z_]+) = \((\d+), (\d+)\)$", source, re.M):
            self.assertLess(int(x), opencore.GUEST_WIDTH, f"{name} x")
            self.assertLess(int(y), opencore.GUEST_HEIGHT, f"{name} y")
