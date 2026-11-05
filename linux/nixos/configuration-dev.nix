# NixOS dev image: the toolchain and the settings a driven machine needs.
#
# This is the last stage of the NixOS chain:
#
#     base (installed from the ISO)  ->  derive (a desktop)  ->  dev (this)
#
# and it exists for the same reason it does on every other system here: a base or derived
# image should be a picture of NixOS, and whatever the pipeline puts on top should be a stage
# of its own, with its own tag, re-runnable without rebuilding the system under it.
#
# WHAT IS HERE THAT IS NOT A PACKAGE
#
# The settings matter as much as the packages and they are the ones a VM needs rather than a
# workstation:
#
#   no screen blanking, no lock screen   the guest is photographed to decide whether it
#                                        booted, and a screen that blanks after ten minutes
#                                        is indistinguishable from a machine that died
#   no automatic suspend                 there is no lid to close and nothing to suspend to
#   no nix auto-upgrade                  a channel update that lands mid-build changes the
#                                        toolchain underneath it
#
# EACH OF THOSE IS OFF IN THE BASE TOO, and this repeats them so that a dev disk made from
# somebody else's derived image is still correct. `mkDefault` keeps the base's value when the
# base set one, because a setting that silently overrode an explicit choice in
# configuration-minimal.nix would be this file winning an argument it was not part of.
#
# IT IMPORTS THE DERIVED CONFIGURATION, NOT THE MINIMAL ONE. A dev disk is a desktop with a
# toolchain; importing configuration-minimal.nix here would build a headless image and then
# call it a desktop.
{ config, lib, pkgs, ... }:

{
  # THE DERIVED CONFIGURATION COMES IN AS A FILE BESIDE THIS ONE. derive.py writes
  # configuration.nix for the desktop and this replaces it, so what it has to keep is
  # everything that file pulled in - see the note in configuration-desktop.nix about a lost
  # fileSystems entry being an unbootable disk. ./configuration-desktop.nix is streamed next
  # to this file by lib/lindev.py for that reason.
  imports = [ ./configuration-desktop.nix ];

  # --- the toolchain ----------------------------------------------------------
  #
  # THE SAME LIST THE OTHER SYSTEMS INSTALL, in NixOS's names. A disk from this repository is
  # driven over ssh afterwards and half of what a runner does needs one of these:
  #
  #   gcc, gnumake, pkg-config   the C toolchain; gcc is what `cc` resolves to
  #   git                        the runner's most basic need, and what flakes reach for
  #   nodejs, npm                the Actions runner is a Node program
  #   python3, pip               jq and many actions shell out to python3
  #   jq, unzip, rsync, file     parsing, unpacking, copying, identifying
  #
  # gcc IS NOT IN A NIXOS PROFILE BY DEFAULT, unlike Ubuntu's build-essential. Without it a
  # dev disk here cannot compile the thing the other three systems' dev disks exist to
  # compile.
  #
  # `nodejs` AND NOT `nodejs npm`, because THERE IS NO npm PACKAGE HERE. nixpkgs builds npm
  # inside the nodejs derivation and puts `npm` and `npx` on PATH with it, so listing it
  # separately fails evaluation outright:
  #
  #     error: undefined variable 'npm'
  #     at /etc/nixos/configuration.nix:58:12
  #         57|     git
  #         58|     nodejs npm
  #             |            ^^^
  #
  # which is run 42060185931 - six dev legs, each after pulling and booting its disk. On
  # Ubuntu the same tools are two apt packages (nodejs, npm) and that is where the list came
  # from; NixOS is not apt.
  environment.systemPackages = with pkgs; [
    gcc gnumake pkg-config
    git
    nodejs
    python3 python3Packages.pip
    jq
    unzip zip xz
    rsync file less wget
  ];

  # --- the settings a VM needs ------------------------------------------------
  #
  # mkDefault ON EVERY ONE, so configuration-minimal.nix and configuration-desktop.nix win
  # when they have an opinion. What these are for is the case where they do not: a derived
  # image from an older run, or a desktop whose own module sets a ten-minute blank.
  # NO `services.xserver.displayManager.gdm.autoSuspend` HERE. It was written from memory and
  # the option name is not one GDM declares - `autoLogin` is, and configuration-desktop.nix
  # already uses that path correctly. An option that does not exist fails nixos-rebuild the
  # same way services.logind.settings did, and the cost is six matrix legs.
  #
  # THE BLANK IS HANDLED WHERE THE DESKTOPS ACTUALLY READ IT:
  #
  #   GNOME        dconf, below - org/gnome/desktop/session idle-delay 0
  #   the rest     they are Wayland compositors that do not idle-lock by default, and the
  #                console blanking below covers the vt they leave behind

  # CONSOLE BLANKING. `2` is "never" for the Linux console; on a guest whose only display is
  # the emulated VGA this is the equivalent of the power plan on Windows.
  boot.consoleLogLevel = lib.mkDefault 4;

  # NO AUTOMATIC UPGRADE. NixOS does not enable it by default, and it is named here so that
  # an image derived from a configuration that did enable it does not keep doing so.
  system.autoUpgrade.enable = lib.mkDefault false;
  system.autoUpgrade.allowReboot = lib.mkDefault false;

  # POWER MANAGEMENT: no suspend, no hibernate. A VM has nothing to resume from and suspend
  # is the state from which a photographed guest never comes back.
  powerManagement.enable = lib.mkDefault false;

  # A LID SWITCH AND A SLEEP KEY EXIST ON SOME EMULATED MACHINES and both are wrong here.
  #
  # `settings.Login`, IN THE nixpkgs THIS REPOSITORY PINS. Read from that commit's
  # nixos/modules/system/boot/systemd/logind.nix rather than from what a build happened to
  # say:
  #
  #     options.services.logind = {
  #       settings.Login = lib.mkOption { ... };        <- the real option
  #       ...
  #     };
  #     imports = [ (lib.mkRemovedOptionModule [ "services" "logind" "extraConfig" ]
  #                   "Use services.logind.settings.Login instead.") ];
  #
  # TWO RUNS, TWO OPPOSITE ERRORS, ONE HOUR APART - and the cause was not this line at all.
  # derive.py built against `nixos-unstable.tar.gz`, a branch:
  #
  #     14:00  error: The option `services.logind.settings' does not exist
  #     15:01  error: `services.logind.extraConfig' no longer has any effect;
  #            Use services.logind.settings.Login instead
  #
  # The first error was accurate for the branch at 14:00 and the second for the branch at
  # 15:01, because the branch changed in between. NIXPKGS_COMMIT in derive.py now pins one
  # tree, so this name stays correct until somebody deliberately bumps the pin - and the
  # comment above says which name to check when they do.
  services.logind.settings.Login = {
    HandleLidSwitch = lib.mkDefault "ignore";
    HandleLidSwitchExternalPower = lib.mkDefault "ignore";
    HandleSuspendKey = lib.mkDefault "ignore";
    HandleHibernateKey = lib.mkDefault "ignore";
  };

  # THE LOCK SCREEN. `gnome`'s settings daemon locks after five minutes of idle by default,
  # and a locked GNOME has no active user session - which is exactly what
  # lib/session.check_wayland_session refuses: Class=greeter and no compositor serving
  # wayland-0. The same shape as Windows' lock page and the same fix.
  #
  # `programs.dconf.profiles`, AND `dconf.settings` IS NOT AN OPTION AT ALL.
  #
  # dconf is a MODULE in NixOS, not a top-level namespace: nixos/modules/programs/dconf.nix
  # declares `programs.dconf.profiles.<name>.databases`, and each database is either a path,
  # a package, or an attrset with a `settings` key. So the whole thing is
  #
  #     programs.dconf.profiles.user.databases = [ { settings = { ... }; } ]
  #
  # A bare `dconf.settings` is what a reader would write from memory - GNOME's own
  # documentation calls the file `dconf`, and the option is `programs.dconf` - and it fails
  # the way run 42044019487 failed, on all six matrix legs:
  #
  #     error: The option `dconf' does not exist. ... Did you mean `boot', `ec2' or `fonts'?
  #
  # THE CONDITION IS `gnome`, NOT `services.xserver.enable`. dconf is only read by GNOME;
  # keying it on xserver meant these settings switched themselves off the moment xserver
  # stopped being enabled for GNOME - which configuration-desktop.nix now does not do,
  # because a Wayland session does not need one.
  programs.dconf.profiles.user.databases = lib.mkIf
    (config.services.desktopManager.gnome.enable or false)
    [{
      settings = {
        "org/gnome/desktop/screensaver" = {
          lock-enabled = false;
          idle-activation-enabled = false;
        };
        # `idle-delay` IS A uint32 AND A BARE 0 DOES NOT SAY SO. GNOME stores dconf values
        # as GVariant and the NixOS module converts Nix values into it by looking at the
        # schema's type; for the numeric types there is no single obvious one, so it refuses
        # to guess and evaluation stops:
        #
        #     error: The GVariant type for number "0" is unclear.
        #     Please wrap the value with one of the following, depending on the value type
        #     in GSettings schema:
        #     - `lib.gvariant.mkInt32` for `i`
        #     - `lib.gvariant.mkUint32` for `u`
        #     ...
        #
        # which is run 42061471565's `dev (gnome)`, and only that leg - the block is behind
        # `config.services.desktopManager.gnome.enable`, so the other five never evaluate it.
        #
        # org.gnome.desktop.session's `idle-delay` is declared `u` in GNOME's
        # org.gnome.desktop.session.gschema.xml, so mkUint32 is the one that matches the
        # schema rather than merely one that evaluates.
        "org/gnome/desktop/session" = { idle-delay = lib.gvariant.mkUint32 0; };
      };
    }];

  # --- and what a dev user expects to exist -----------------------------------
  #
  # The account already exists from the base; these are the group memberships a build needs
  # and the two directories a toolchain creates for itself. `docker` is created by the base,
  # so `users.users.docker` here only adds to it.
  users.users.docker.extraGroups = lib.mkAfter [ "wheel" "video" "render" "input" ];
}
