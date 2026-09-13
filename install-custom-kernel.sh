#!/usr/bin/env bash
# Install a completed, trusted kernel source tree from its working directory.
# Build options recorded by build-custom-kernel.sh are reused verbatim.
# Manually built trees are accepted only when their release matches make's
# current settings. No existing installed release is overwritten.
set -euo pipefail

log()  { printf '\n==> %s\n' "$*"; }
warn() { printf '\n[warn] %s\n' "$*" >&2; }
err()  { printf '\n[error] %s\n' "$*" >&2; exit 1; }

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
run_sudo() {
  local opts=(--preserve-env=DEBIAN_FRONTEND,NEEDRESTART_MODE)
  [[ -z ${SUDO_ASKPASS:-} ]] || opts+=(-A)
  sudo "${opts[@]}" "$@"
}

[[ $EUID -ne 0 ]] || err "Run this as a normal user; it will sudo when needed."
[[ $# -eq 0 ]] || err "Run without arguments from the completed source tree."
mkdir -p "$HOME/kernel-build"
if [[ ${KERNEL_MANAGER_LOCK_FD:-} =~ ^[0-9]+$ ]]; then
  exec 9>&"$KERNEL_MANAGER_LOCK_FD"
else
  exec 9>"$HOME/kernel-build/.operation.lock"
fi
flock -n 9 || err "Another kernel operation is running."

[[ -f Makefile && -d kernel && -s .config && -s vmlinux && -s System.map && -s include/config/kernel.release ]] ||
  err "Incomplete kernel build. Run this inside a successfully built source tree."
MAKE_ARGS=()
if [[ -f .kernel-manager-make-args ]]; then
  [[ -f .kernel-manager-complete && -s .kernel-manager-checksums ]] || err "The recorded build did not finish."
  sha256sum --status -c .kernel-manager-checksums || err "Build artifacts or settings changed. Rebuild before installing."
  mapfile -d '' -t MAKE_ARGS < .kernel-manager-make-args
  for arg in "${MAKE_ARGS[@]}"; do
    [[ $arg =~ ^(LOCALVERSION|KCFLAGS|CC|LLVM|LLVM_IAS)= ]] || err "Invalid saved make argument."
  done
fi
RELEASE=$(cat include/config/kernel.release)
[[ $RELEASE =~ ^[0-9]+\.[0-9]+[a-zA-Z0-9._+-]*$ ]] || err "Invalid kernel release."
[[ "$RELEASE" != "$(uname -r)" ]] || err "Refusing to replace the running kernel."
[[ $(make -s "${MAKE_ARGS[@]}" kernelrelease) == "$RELEASE" ]] ||
  err "Build/install release mismatch. Rebuild with recorded toolchain settings."
IMAGE=$(make -s "${MAKE_ARGS[@]}" image_name)
[[ "$IMAGE" == arch/* && "$IMAGE" != *..* && -s "$IMAGE" ]] || err "No completed kernel boot image."
if [[ -f .kernel-manager-make-args ]]; then
  sha256sum --status -c .kernel-manager-checksums || err "Configuration changed while checking the build. Rebuild before installing."
fi
for target in "/lib/modules/$RELEASE" "/boot/vmlinuz-$RELEASE" "/boot/config-$RELEASE" "/boot/System.map-$RELEASE" "/boot/initrd.img-$RELEASE" "/boot/initramfs-$RELEASE.img"; do
  [[ ! -e "$target" && ! -L "$target" ]] || err "Already exists: $target. Refusing to overwrite this release; use a different --localversion."
done

# Fail before touching system files if this boot layout is unsupported.
command -v installkernel >/dev/null || err "No installkernel helper; this distribution needs a manual kernel installation."
if command -v update-initramfs >/dev/null; then
  INITRAMFS=update-initramfs
  INITRD="/boot/initrd.img-$RELEASE"
elif command -v dracut >/dev/null; then
  INITRAMFS=dracut
  INITRD="/boot/initramfs-$RELEASE.img"
else
  err "No supported initramfs generator (update-initramfs or dracut). Install manually for this distribution."
fi
if [[ -f /boot/grub/grub.cfg ]] && command -v update-grub >/dev/null; then
  GRUB=(update-grub)
  GRUB_CFG=/boot/grub/grub.cfg
elif [[ -f /boot/grub/grub.cfg ]] && command -v grub-mkconfig >/dev/null; then
  GRUB=(grub-mkconfig -o /boot/grub/grub.cfg)
  GRUB_CFG=/boot/grub/grub.cfg
elif [[ -f /boot/grub2/grub.cfg ]] && command -v grub2-mkconfig >/dev/null; then
  GRUB=(grub2-mkconfig -o /boot/grub2/grub.cfg)
  GRUB_CFG=/boot/grub2/grub.cfg
else
  err "No supported existing GRUB configuration. Install manually for this bootloader."
fi
if [[ -r "$GRUB_CFG" ]]; then
  GRUB_TEXT=$(cat "$GRUB_CFG")
else
  GRUB_TEXT=$(run_sudo cat "$GRUB_CFG")
fi
if grep -Eq '^[[:space:]]*blscfg([[:space:]]|$)' <<< "$GRUB_TEXT"; then
  err "BLS installations require the distribution kernel-install tooling. Install manually."
fi
BOOT_FREE=$(df -Pk /boot | awk 'END {print $4}')
BOOT_NEEDED=$(( $(du -k "$IMAGE" | cut -f1) + 262144 ))
(( BOOT_FREE >= BOOT_NEEDED )) || err "Insufficient /boot space for the image and initramfs (256 MiB reserve)."
MODULE_KB=$(find . -type f -name '*.ko' -printf '%s\n' | awk '{n+=$1} END {printf "%.0f", n/1024+65536}')
MODULE_FREE=$(df -Pk /lib/modules | awk 'END {print $4}')
(( MODULE_FREE >= MODULE_KB )) || err "Insufficient space for kernel modules."

if command -v mokutil >/dev/null && mokutil --sb-state 2>/dev/null | grep -qi enabled; then
  warn "Secure Boot is enabled. Sign this kernel and its modules before attempting to boot it."
fi
log "Installing $RELEASE; failures after this point may require manual cleanup"
trap 'warn "Installation failed or was interrupted. Do not reboot until /boot, modules, and GRUB have been checked."' ERR
if grep -qx 'CONFIG_MODULES=y' .config; then
  run_sudo make "${MAKE_ARGS[@]}" modules_install
fi
run_sudo make "${MAKE_ARGS[@]}" install
[[ -s "/boot/vmlinuz-$RELEASE" && -s "/boot/config-$RELEASE" && -s "/boot/System.map-$RELEASE" ]] ||
  err "installkernel did not create the expected boot files. Check the installation manually."
if [[ "$INITRAMFS" == update-initramfs ]]; then
  MODE=-c
  [[ ! -e "$INITRD" ]] || MODE=-u
  run_sudo update-initramfs "$MODE" -k "$RELEASE"
else
  run_sudo dracut --force "$INITRD" "$RELEASE"
fi
[[ -s "$INITRD" ]] || err "No initramfs was produced. Do not reboot into this kernel."
run_sudo "${GRUB[@]}"
# BLS configurations may keep entries in separate files rather than grub.cfg.
if ! run_sudo grep -Fq -- "$RELEASE" "$GRUB_CFG"; then
  err "Could not verify a GRUB entry for $RELEASE. Check your boot entries manually before rebooting."
fi
log "Installed $RELEASE"
echo "Verify the GRUB menu, Secure Boot signatures, and a working fallback before rebooting."
echo "After rebooting, confirm with: uname -r"
