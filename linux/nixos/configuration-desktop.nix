# NixOS desktop image: a normal desktop distribution, built from the minimal
# base by changing this file and running nixos-rebuild (derive.sh).
#
# DESKTOP is substituted at derive time: labwc | gnome | kde | cosmic.
#
# What belongs here is what a desktop distribution itself provides: the desktop
# session, its display manager, the audio stack, clipboard tools. What does NOT
# belong here is anything specific to the sandbox - no nix-ld, no prebuilt
# binaries, no launcher unit. Those are layered on afterwards, when a user
# builds their own image, so this disk stays a plain desktop system.
{ config, lib, pkgs, ... }:

let
  desktop = "@DESKTOP@";
in
{
  # Reuse the base image definition wholesale rather than restating filesystems,
  # boot loader, users and ssh. derive.sh replaces /etc/nixos/configuration.nix on
  # the installed base disk, so whatever this file does not import, the system
  # loses - and a lost fileSystems entry is an unbootable disk. Importing the
  # minimal configuration keeps the two in step by construction: the same file
  # builds the base and forms the foundation of every desktop.
  imports = [ ./configuration-minimal.nix ];
  # --- audio -----------------------------------------------------------------
  #
  # Part of the distribution, not of the sandbox: a desktop with no sound is not
  # a desktop. wireplumber is the session manager - without it pipewire starts
  # and no nodes are ever created - and rtkit is what lets those nodes take
  # realtime priority.
  services.pipewire = {
    enable = true;
    alsa.enable = true;
    pulse.enable = true;
  };
  services.pipewire.wireplumber.enable = true;
  security.rtkit.enable = true;

  hardware.graphics = { enable = true; enable32Bit = false; };

  # The DRM kernel modules. hardware.graphics enables the userspace stack (mesa,
  # libdrm) but does not load a driver for the emulated display, and wlroots
  # aborts with
  #   Failed to find any DRM render node
  # when there is no /dev/dri to render into - which is exactly what a bare
  # labwc session showed on the first derived disk: the compositor started and
  # then died before painting anything.
  #
  # bochs covers QEMU's default VGA; virtio_gpu covers the -device virtio-gpu
  # variant; both are tiny.
  boot.initrd.availableKernelModules = [ "bochs" "virtio_gpu" "drm" ];
  boot.kernelModules = [ "bochs" "virtio_gpu" ];

  # Two packages, both tools rather than scaffolding.
  #
  # wl-clipboard: wl-copy and wl-paste, the Wayland clipboard. A desktop needs it
  # and easystream reports clipboard=Wl only when wl-copy exists.
  #
  # kitty: a terminal. One client is enough to see whether a compositor is
  # drawing at all, and kitty is the one hyprland's own default config binds to
  # SUPER+Q. xclip is gone - it is the X11 clipboard and this is a Wayland disk;
  # foot duplicated kitty; fuzzel is a launcher and nothing here clicks.
  environment.systemPackages = with pkgs; [
    wl-clipboard
    kitty
  ];


  # --- the desktop ------------------------------------------------------------
  #
  # These differ only in which session the display manager starts and which
  # portal backend is loaded, which is what xdg-desktop-portal keys on: each
  # .portal file declares UseIn=<desktop> and a mismatched XDG_CURRENT_DESKTOP
  # leaves the portal exposing only FileChooser and Settings.
  # NO services.xserver.enable AT ALL, AND THAT IS THE CORRECT SETTING FOR ALL SIX.
  #
  # This said `lib.mkIf (desktop == "gnome" || desktop == "kde") true` and it was wrong for
  # both of them. Read from nixpkgs master rather than remembered:
  #
  #     nixos/modules/services/desktop-managers/gnome.nix
  #         the ONLY xserver reference is `services.xserver.updateDbusEnvironment = true`,
  #         in the X11-*helpers* path; nothing reads xserver.enable
  #     nixos/modules/services/desktop-managers/plasma6.nix
  #         the ONLY reference is `config.services.xserver.xkb.dir`, a path into the
  #         keyboard-configuration tree, not a requirement that X run
  #
  # Both modules live under services/desktop-managers now, not under services/x11/ - they
  # were moved when they stopped being X11 desktop managers. A Wayland session does not need
  # an X server; it needs a display manager and a session, which the lines below provide.
  #
  # ENABLING IT ALSO ENABLES A LOGIN SCREEN THIS IMAGE DOES NOT WANT: X starts, GDM or SDDM
  # draws an X11 greeter, and the guest boots into something that is not the session the
  # launch stage looks for. Every one of these six desktops is Wayland.
  #
  # WHAT WOULD ACTUALLY NEED IT: an X11-only application run from inside the session, through
  # XWayland. XWayland is a session feature and does not require xserver.enable either.

  services.desktopManager.gnome.enable = lib.mkIf (desktop == "gnome") true;
  services.displayManager.gdm.enable = lib.mkIf (desktop == "gnome") true;
  # THE OPTION'S REAL NAME, NOT ITS ALIAS.
  #
  # `services.xserver.displayManager.gdm.autoLogin` is registered in gdm.nix as a RENAME to
  # `services.displayManager.gdm.autoLogin` - it works, it emits a warning, and after
  # xserver goes away it is a path into a tree nothing else in this file uses. Same option,
  # named where it is declared.
  services.displayManager.gdm.autoLogin = lib.mkIf (desktop == "gnome") {
    enable = true;
    user = "docker";
  };

  services.desktopManager.plasma6.enable = lib.mkIf (desktop == "kde") true;
  # SDDM for KDE and for the three compositors that register a session but ship
  # no display manager. One definition, because Nix counts two as a conflict.
  services.displayManager.sddm.enable =
    lib.mkIf (desktop == "kde" || builtins.elem desktop [ "hyprland" "miracle" ]) true;

  services.desktopManager.cosmic.enable = lib.mkIf (desktop == "cosmic") true;
  services.displayManager.cosmic-greeter.enable = lib.mkIf (desktop == "cosmic") true;
  # GENERIC autologin, shared by every greeter that reads it: SDDM and the
  # COSMIC greeter both do.
  #
  # Only gdm is different - it declares autoLogin under
  # services.xserver.displayManager.gdm, above.
  #
  # This is the option that matters for KDE. Its mdm-specific spelling
  # [services.displayManager.sddm.autoLogin] is registered in sddm.nix as a
  # rename to this one, so using it emitted a warning AND never logged anyone in:
  # the screenshot was SDDM's own login screen - time, docker avatar, Password
  # box - which is what every earlier "wallpaper but no panel" reading really was.
  services.displayManager.autoLogin =
    lib.mkIf (builtins.elem desktop [ "cosmic" "kde" "hyprland" "miracle" ]) {
      enable = true;
      user = "docker";
    };

  # greetd, for labwc only.
  #
  # labwc installs the packages and registers no session, unlike hyprland
  # and miracle-wm - each of those sets services.displayManager.sessionPackages
  # itself. So the other three are ordinary entries in whatever display manager
  # is enabled, and only labwc needs a DM that can be handed a raw command.
  #
  # initial_session, NOT default_session, AND RUN 41650668823 IS WHY.
  #
  # greetd has two kinds of session and logind does not treat them the same:
  #
  #     default_session   the GREETER. greetd runs it before anyone logs in, and
  #                       logind registers it with Class=greeter
  #     initial_session   an automatic login. greetd starts it as the user at boot
  #                       and PAM registers it with Class=user
  #
  # This file used default_session, so the labwc session was a greeter session no
  # matter which user it ran as. The launch stage asked for an active session with
  # Class=user or manager and correctly found none:
  #
  #     sessions (id|type|class|active):
  #         ['1|wayland|greeter|yes', '2|unspecified|manager|yes', ...]
  #     === NO ACTIVE USER WAYLAND SESSION ===
  #
  # and the screenshot was mean grey 0.0 - there was no desktop, because greetd was
  # still waiting for a login at a greeter it had been told to run as the compositor
  # itself.
  #
  # initial_session is greetd's autologin, which is exactly what this disk wants: the
  # compositor starts at boot as docker, with no password and no greeter in between.
  # default_session stays as the fallback for after a logout, which is what greetd
  # falls back to once the initial session ends.
  services.greetd = lib.mkIf (desktop == "labwc") {
    enable = true;
    settings = {
      # THE AUTOLOGIN. This is the session the launch stage sees.
      initial_session = {
        user = "docker";
        command = "${pkgs.writeShellScript "labwc-session" ''
          # THE RUNTIME DIRECTORY IS THE USER'S OWN, LOOKED UP RATHER THAN ASSUMED.
          #
          # It was hardcoded to /run/user/1000. That is docker's uid on every image in
          # this repository because configuration-minimal.nix creates it as the first
          # normal user - but a session that hardcodes another process's uid is one
          # rebuild away from a compositor writing its socket where nothing looks for it.
          export XDG_RUNTIME_DIR="/run/user/$(id -u)"
          export XDG_SESSION_TYPE=wayland
          export XDG_CURRENT_DESKTOP=wlroots
          # DRM, not headless: headless output is never drawn to the framebuffer.
          export WLR_BACKENDS=drm
          export WLR_LIBINPUT_NO_DEVICES=1
          # pixman: wlroots wants a DRM render node; this guest has card0 only.
          export WLR_RENDERER=pixman
          # No autostart here either. This used to open foot so the screenshot had
          # something to look at, which is the same mistake as the systemd unit I
          # put on the other three: a gold image that launches a terminal is not a
          # stock desktop. smoke.sh starts kitty over the serial console instead,
          # after boot, where it belongs.
          exec ${pkgs.labwc}/bin/labwc
        ''}";
      };
      # default_session IS REQUIRED, AND LEAVING IT OUT CRASHES greetd.
      #
      # The comment that used to sit here said the module supplies a default when the
      # attribute is absent. It does not - greetd validates its own configuration at
      # startup and refuses one with no default session:
      #
      #     greetd[615]: default_session contains no command
      #     greetd.service: Main process exited, code=exited, status=1/FAILURE
      #
      # which is run 41757495583's labwc leg: greetd exited in five seconds, no session
      # was ever created, and the launch stage correctly reported none. The other five
      # desktops were unaffected because only labwc uses greetd at all.
      #
      # agreety ships with greetd, so this adds no package: it is the login prompt shown
      # after the autologin session ends, and it needs no `--cmd` - that flag belongs to
      # tuigreet, not to this one.
      # NO --cmd ON agreety. `--cmd` is tuigreet's flag; agreety takes its session from
      # greetd's own session file and ignores anything after its own name, so passing one
      # would be a lie that looks like configuration. agreety is a login prompt: it asks
      # for a name and a password and then starts the session that user's shell is
      # registered for, which is what this disk wants if the autologin ever ends.
      default_session = {
        command = "${pkgs.greetd}/bin/agreety";
        user = "greeter";
      };
    };
  };

  # SDDM for hyprland and miracle-wm.
  #
  # Not greetd: the generic session machinery lives in display-managers/default.nix
  # and greetd.nix implements none of it - no sessionData, no sessionPackages, no
  # defaultSession. Their modules register a .desktop in sessionPackages, so a DM
  # that reads that list is what actually starts them. SDDM does, and it is not
  # tied to a desktop the way gdm is to GNOME or cosmic-greeter to COSMIC.
  # SDDM MUST BE TOLD WHICH DISPLAY SERVER TO RUN, AND NOW THAT IS EVERY DESKTOP IT SERVES.
  #
  # sddm.nix asserts, at line 350:
  #
  #     assertion = xcfg.enable || cfg.wayland.enable;
  #     "SDDM requires either services.xserver.enable or
  #      services.displayManager.sddm.wayland.enable to be true"
  #
  # This used to apply it to hyprland and miracle-wm only, on the reasoning stated in the
  # comment that was here: "KDE gets xserver.enable from plasma6.nix". THAT WAS NEVER TRUE.
  # plasma6.nix's only xserver reference is `config.services.xserver.xkb.dir`, a path into
  # the keyboard tree. Nothing in it sets xserver.enable - so KDE would have tripped this
  # assertion the moment xserver.enable stopped being set for it, which is exactly what this
  # commit does.
  #
  # All three are Wayland, so all three get the Wayland backend. KDE's SDDM then starts the
  # wayland greeter with kwin as its compositor, which is what Plasma 6 does by default on a
  # distribution that does not force X.
  services.displayManager.sddm.wayland.enable =
    lib.mkIf (builtins.elem desktop [ "kde" "hyprland" "miracle" ]) true;

  # One definition again: a third spelling of this attribute would conflict.
  services.displayManager.defaultSession =
    if desktop == "hyprland" then "hyprland"
    else if desktop == "miracle" then "miracle-wm"
    else null;

  programs.labwc.enable = lib.mkIf (desktop == "labwc") true;
  # --- two more compositors, each with its own standard module ----------------
  #
  # hyprland: forked from wlroots and now its own compositor, but it still
  # implements zwlr_screencopy and zwlr_virtual_pointer for the ecosystem, and it
  # also speaks libei. Either of easystream's paths could work here.
  #
  #
  # miracle-wm: Mir based. Its module says plainly "No portal support yet", so if
  # anything works it will be a wlr protocol.
  programs.hyprland.enable = lib.mkIf (desktop == "hyprland") true;
  programs.wayland.miracle-wm.enable = lib.mkIf (desktop == "miracle") true;

  # Portal backends, one per desktop that has one.
  #
  # KDE is absent on purpose: plasma6.nix already sets extraPortals itself (and
  # its packages live under kdePackages after the Plasma 6 move).
  #
  # miracle-wm is absent because it HAS no portal backend - its module sets
  # enableWlrPortal and enableGtkPortal to false with a link to the open issue -
  # and leaving xdg.portal.enable on for it fails the assertion
  #   Setting xdg.portal.enable to true requires a portal implementation
  #
  # hyprland gets its own from portal-hyprland, which its module sets up when
  # enabled.
  xdg.portal.enable = lib.mkIf (desktop != "miracle") true;
  xdg.portal.extraPortals =
    lib.optionals (desktop == "gnome") [ pkgs.xdg-desktop-portal-gnome ]
    ++ lib.optionals (desktop == "cosmic") [ pkgs.xdg-desktop-portal-cosmic ];
  xdg.portal.configPackages =
    lib.optionals (desktop == "gnome") [ pkgs.xdg-desktop-portal-gnome ]
    ++ lib.optionals (desktop == "cosmic") [ pkgs.xdg-desktop-portal-cosmic ]
    ++ lib.optionals (desktop == "hyprland") [ pkgs.xdg-desktop-portal-hyprland ];
}
