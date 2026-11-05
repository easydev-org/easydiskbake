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
  environment.systemPackages = with pkgs; [
    gcc gnumake pkg-config
    git
    nodejs npm
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
  # `extraConfig`, NOT `settings.Login`, AND IT IS A STRING. NixOS has no
  # services.logind.settings - the module exposes `extraConfig`, which is appended to the
  # generated logind.conf verbatim. The attrset version is from systemd's own documentation
  # and from other modules that do have a `settings` option, and it fails the way a wrong
  # option name always does here:
  #
  #     error: The option `services.logind.settings' does not exist.
  #     Did you mean `boot', `ec2' or `fonts'?
  #
  # which is run 42034715730 - all six dev matrix legs, at nixos-rebuild, after their disks
  # had been pulled.
  #
  # `lib.mkDefault` DOES NOT APPLY TO A STRING, so this is a plain assignment. That is
  # correct here: a configuration that already set extraConfig would win by defining the
  # same option, and merging two strings is not what this needs.
  services.logind.extraConfig = ''
    HandleLidSwitch=ignore
    HandleLidSwitchExternalPower=ignore
    HandleSuspendKey=ignore
    HandleHibernateKey=ignore
  '';

  # THE LOCK SCREEN. `gnome`'s settings daemon locks after five minutes of idle by default,
  # and a locked GNOME has no active user session - which is exactly what
  # lib/session.check_wayland_session refuses: Class=greeter and no compositor serving
  # wayland-0. The same shape as Windows' lock page and the same fix.
  #
  # THE CONDITION IS `gnome`, NOT `services.xserver.enable`. dconf is only read by GNOME;
  # keying it on xserver meant these settings switched themselves off the moment xserver
  # stopped being enabled for GNOME - and configuration-desktop.nix does not enable it any
  # more, because a Wayland session does not need one. A guard that silently stops guarding
  # is worse than no guard.
  dconf.settings = lib.mkIf (config.services.desktopManager.gnome.enable or false) {
    "org/gnome/desktop/screensaver" = { lock-enabled = false; idle-activation-enabled = false; };
    "org/gnome/desktop/session" = { idle-delay = lib.mkDefault 0; };
  };

  # --- and what a dev user expects to exist -----------------------------------
  #
  # The account already exists from the base; these are the group memberships a build needs
  # and the two directories a toolchain creates for itself. `docker` is created by the base,
  # so `users.users.docker` here only adds to it.
  users.users.docker.extraGroups = lib.mkAfter [ "wheel" "video" "render" "input" ];
}
