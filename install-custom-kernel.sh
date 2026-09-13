#!/usr/bin/env bash
#
# install-custom-kernel.sh
#
# Installs an already-built kernel: runs `make modules_install` and
# `make install`, then updates the bootloader (GRUB). This is the last
# step after build-custom-kernel.sh has finished compiling successfully —
# split out separately so you can re-run just the install step without
# rebuilding, or install a kernel you built manually.
#
# USAGE:
#   Run this from inside the kernel source directory you just built, e.g.:
#     cd ~/kernel-build/linux-7.1.3
#     ~/Kernel/install-custom-kernel.sh
#
set -euo pipefail

log()  { echo -e "\n\033[1;32m==>\033[0m $*"; }
warn() { echo -e "\n\033[1;33m[warn]\033[0m $*"; }
err()  { echo -e "\n\033[1;31m[error]\033[0m $*"; exit 1; }

# Transparent sudo wrapper: if SUDO_ASKPASS is set (the GUI app sets this so
# it can supply a password dialog instead of a terminal prompt), use `-A` to
# read the password from that helper. Otherwise behaves exactly like a plain
# `sudo` call, so nothing changes when this script is run directly from a
# terminal.
run_sudo() {
  if [[ -n "${SUDO_ASKPASS:-}" ]]; then
    sudo -A "$@"
  else
    sudo "$@"
  fi
}

[[ $EUID -eq 0 ]] && err "Run this as a normal user (it will sudo when needed), not as root."

# Prevent apt/dpkg/needrestart from popping up interactive dialogs (e.g. the
# initramfs/update-grub steps can trigger a "restart services?" prompt).
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Ask for the sudo password once up front, then keep it alive in the
# background so a slow modules_install/mkinitramfs/grub run never stops to
# re-prompt partway through.
log "Requesting sudo access up front (used throughout the script)"
run_sudo -v
( while true; do sudo -n true; sleep 60; kill -0 "$$" 2>/dev/null || exit; done ) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT

# Sanity check: make sure we're actually inside a built kernel source tree
[[ -f "Makefile" && -d "kernel" && -f ".config" ]] || \
  err "This doesn't look like a kernel source directory (no Makefile/.config found).
  cd into the linux-<version> directory you built first, e.g.:
    cd ~/kernel-build/linux-7.1.3"

[[ -f "vmlinux" || -f "arch/x86/boot/bzImage" ]] || \
  warn "Couldn't find a built vmlinux/bzImage — did the build actually finish? Continuing anyway."

log "Installing kernel modules (requires sudo)"
run_sudo make modules_install

log "Installing kernel image, config, System.map, and updating initramfs (requires sudo)"
run_sudo make install

log "Updating bootloader"
if command -v update-grub >/dev/null 2>&1; then
  run_sudo update-grub
elif [[ -f /etc/default/grub ]] && command -v grub-mkconfig >/dev/null 2>&1; then
  run_sudo grub-mkconfig -o /boot/grub/grub.cfg
elif command -v grub2-mkconfig >/dev/null 2>&1; then
  run_sudo grub2-mkconfig -o /boot/grub2/grub.cfg
else
  warn "Couldn't detect a GRUB update command — update your bootloader config manually."
fi

# Secure Boot heads-up, since an unsigned custom kernel commonly won't boot
if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi "enabled"; then
  warn "Secure Boot is ENABLED. This kernel is unsigned, so it may refuse to boot."
  warn "If the new entry fails at boot, either disable Secure Boot temporarily,"
  warn "or sign the kernel/modules yourself via MOK enrollment."
fi

log "Done!"
echo "Your previous kernel remains available in the GRUB boot menu as a fallback."
echo "Reboot and select the new kernel entry to test it:"
echo "    sudo reboot"
echo
echo "After rebooting, confirm with:  uname -r"
