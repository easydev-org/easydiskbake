# Minimal NixOS base image for the sandbox family.
#
# This is the ONE disk that is installed from the ISO. Every desktop disk is
# derived from it by changing configuration and running nixos-rebuild (see
# derive.sh and configuration-desktop.nix), because partition/format/unpack is
# identical across flavours and nixos-install pays for it every time.
#
# It is deliberately plain: kernel, systemd, ssh, a login user. No desktop, no
# audio stack, no nix-ld, no sandbox services. Anything desktop-shaped belongs
# in configuration-desktop.nix, and anything sandbox-shaped is layered on later
# when a user builds their own image.
{ modulesPath, lib, pkgs, ... }:

{
  imports = [ "${modulesPath}/profiles/minimal.nix" ];

  # --- filesystem / boot -----------------------------------------------------
  #
  # A PARTITION, not the bare disk: grub-install refuses to embed core.img in
  # the MBR gap when a filesystem starts at sector 0 ('will not proceed with
  # blocklists'). The bake formats vda1 and leaves the MBR free. The root is
  # addressed by LABEL because the UUID only exists at install time and would
  # pin the disk to one bake.
  fileSystems."/" = {
    device = "/dev/disk/by-label/nixos";
    fsType = "ext4";
  };

  boot.initrd.availableKernelModules = [
    "virtio_pci" "virtio_blk" "virtio_scsi" "ext4"
  ];
  boot.kernelModules = [ "virtio_pci" "virtio_blk" ];

  boot.loader.grub = {
    enable = true;
    device = "/dev/vda";
    # Also send GRUB to the serial port. Without this the splash goes to the
    # emulated VGA buffer, which nothing in the bake can read, so the bake sees
    # 'Welcome to GRUB!' and then silence whether the kernel started or GRUB
    # gave up.
    extraConfig = ''
      serial --unit=0 --speed=115200
      terminal_output serial
    '';
  };
  boot.loader.timeout = 0;
  boot.kernelParams = [ "console=ttyS0,115200n8" ];

  # --- accounts --------------------------------------------------------------
  services.openssh = {
    enable = true;
    settings.PermitRootLogin = "yes";
    settings.PasswordAuthentication = true;
  };
  services.getty.autologinUser = "docker";

  users.users.docker = {
    isNormalUser = true;
    # video/render/input/audio are here because every derived desktop image
    # needs them; they cost nothing on a headless base and save the desktop
    # configuration from having to redefine the user.
    extraGroups = [ "wheel" "video" "render" "input" "audio" ];
    password = "admin";
  };
  security.sudo.wheelNeedsPassword = false;

  # root needs a real password: the bake installs with --no-root-password, which
  # LOCKS the account, and when a boot goes wrong systemd then cannot even open
  # an emergency console ('the root account is locked').
  users.users.root.password = "admin";

  networking.hostName = "nixos";
  networking.firewall.enable = false;

  # Fixed on purpose: this records the release the system was FIRST installed
  # with, not the one it runs. Bumping it on every nixpkgs update would change
  # which state migrations the activation script applies.
  system.stateVersion = "26.05";

  # --- basics every image needs, desktop or not ------------------------------
  environment.systemPackages = with pkgs; [
    curl vim htop
    findutils gnugrep gawk which
  ];

  # Deliberately absent, and each for a reason:
  #
  #   services.pipewire / wireplumber / rtkit  - audio, desktop only
  #   wl-clipboard, xclip                     - clipboard, desktop only
  #   desktopManager.* / displayManager.*     - that is what derive adds
  #   xdg.portal                              - asserted to need a backend
  #   programs.nix-ld + libopus/x264/...      - only for prebuilt binaries,
  #                                             and a user adds those to their
  #                                             own image when they need them
  #   any sandbox systemd unit                - layered on later, not baked in
}
