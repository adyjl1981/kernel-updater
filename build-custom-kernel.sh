#!/usr/bin/env bash
#
# build-custom-kernel.sh
#
# Detects your hardware, installs the packages needed to build a kernel,
# downloads the latest STABLE mainline Linux kernel source from kernel.org,
# and builds it with a performance-tuned toolchain (CPU-specific flags,
# optional LTO, optional Clang/LLD, ccache).
#
# Tested target distros: Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman)
#
# USAGE:
#   chmod +x build-custom-kernel.sh
#   ./build-custom-kernel.sh              # GCC build, -march=native, ccache
#   ./build-custom-kernel.sh --clang      # Clang+LLD build with LTO (thin)
#   ./build-custom-kernel.sh --jobs 8     # override parallel job count
#
# IMPORTANT SAFETY NOTES:
#   - This will NOT overwrite your existing kernel. `make install` adds a new
#     entry; GRUB keeps the old one as a fallback (in case the new kernel
#     doesn't boot).
#   - Building takes a long time (30 min - a few hours depending on CPU) and
#     a lot of disk space (15-25 GB free recommended).
#   - Secure Boot: an unsigned custom kernel will likely fail to boot with
#     Secure Boot enabled. The script detects this and warns you.
#   - Test in a VM first if you're not comfortable debugging boot issues.
#
set -euo pipefail

# Directory this script itself lives in, so we can find its companion
# install-custom-kernel.sh regardless of where the user invokes it from.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"

### ---------- 0. Options ----------------------------------------------------
USE_CLANG=false
USE_LTO=false
FORCE=false
FULL_DEBUG_INFO=false
JOBS=""
LOCALVERSION="-custom"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clang) USE_CLANG=true; shift ;;
    --lto) USE_LTO=true; shift ;;
    --force) FORCE=true; shift ;;
    --full-debug-info) FULL_DEBUG_INFO=true; shift ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --localversion) LOCALVERSION="$2"; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--clang] [--lto] [--force] [--full-debug-info] [--jobs N] [--localversion -mytag]"
      echo "  --lto              requires --clang. Not recommended on low-RAM/low-core machines."
      echo "  --force            rebuild even if this is the same version you last built."
      echo "  --full-debug-info  keep full debug info (DWARF + BTF). By default this script"
      echo "                     strips it, since the final vmlinux link step needs a lot more"
      echo "                     RAM with debug info enabled and commonly gets OOM-killed on"
      echo "                     low-memory machines. Only pass this on a machine with plenty"
      echo "                     of RAM (8GB+) if you actually need kernel debug symbols/BTF."
      exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

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

if $USE_LTO && ! $USE_CLANG; then
  err "--lto requires --clang (this script only wires up Clang ThinLTO). Add --clang too."
fi

# Prevent apt/dpkg/needrestart from popping up interactive dialogs (e.g. the
# "which services should be restarted?" whiptail prompt) during package
# installs, so the script never stops waiting for a keypress.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Ask for the sudo password once up front, then keep the credential alive
# in the background for the rest of the (potentially multi-hour) run, so a
# timed-out sudo session never stops the script to re-prompt partway through
# a build. The background refresher is killed automatically on exit.
log "Requesting sudo access up front (used throughout the script)"
run_sudo -v
( while true; do sudo -n true; sleep 60; kill -0 "$$" 2>/dev/null || exit; done ) &
SUDO_KEEPALIVE_PID=$!
trap 'kill "$SUDO_KEEPALIVE_PID" 2>/dev/null || true' EXIT

### ---------- 1. Detect distro / package manager ----------------------------
log "Detecting distro and package manager"

if   command -v apt    >/dev/null 2>&1; then PKG=apt
elif command -v dnf    >/dev/null 2>&1; then PKG=dnf
elif command -v pacman >/dev/null 2>&1; then PKG=pacman
else err "Unsupported distro: no apt, dnf, or pacman found."
fi
echo "Package manager: $PKG"

### ---------- 2. Detect hardware --------------------------------------------
log "Detecting CPU / hardware"

CPU_MODEL=$(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | sed 's/^ //')
CPU_VENDOR=$(grep -m1 "vendor_id"  /proc/cpuinfo | cut -d: -f2 | sed 's/^ //')
NPROC=$(nproc)
MEM_GB=$(( $(grep MemTotal /proc/meminfo | awk '{print $2}') / 1024 / 1024 ))
ARCH=$(uname -m)

echo "  CPU:        $CPU_MODEL"
echo "  Vendor:     $CPU_VENDOR"
echo "  Arch:       $ARCH"
echo "  CPU cores:  $NPROC"
echo "  RAM:        ${MEM_GB} GB"

[[ "$ARCH" != "x86_64" && "$ARCH" != "aarch64" ]] && \
  warn "Untested architecture ($ARCH). Script assumes x86_64/aarch64 conventions."

if [[ -z "$JOBS" ]]; then
  JOBS=$NPROC
fi

if (( MEM_GB < 8 )); then
  warn "Less than 8GB RAM detected. Kernel builds are RAM-hungry with high -j values."
  warn "Consider lowering --jobs if you hit OOM / swap thrashing (e.g. --jobs $((NPROC/2)))."
fi

# Detect microarchitecture for GCC/Clang -march tuning (best-effort, x86_64 only)
MARCH="native"
if [[ "$ARCH" == "x86_64" ]] && command -v gcc >/dev/null 2>&1; then
  if gcc -march=native -E -v - </dev/null 2>&1 | grep -q "march=native"; then
    MARCH="native"
  fi
fi
echo "  Using -march=$MARCH -mtune=$MARCH (auto-detected for this CPU)"

# Secure Boot check
if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi "enabled"; then
  warn "Secure Boot is ENABLED. An unsigned custom kernel may refuse to boot."
  warn "You'll need to either disable Secure Boot, or sign the kernel/modules yourself (MOK enrollment) after the build."
fi

### ---------- 3. Install build dependencies ---------------------------------
log "Installing build dependencies via $PKG"

case "$PKG" in
  apt)
    run_sudo apt update
    run_sudo apt install -y \
      build-essential libncurses-dev bison flex libssl-dev libelf-dev \
      bc dwarves git fakeroot rsync cpio kmod ccache \
      libudev-dev pahole zstd libdw-dev gawk
    $USE_CLANG && run_sudo apt install -y clang lld llvm
    ;;
  dnf)
    run_sudo dnf groupinstall -y "Development Tools"
    run_sudo dnf install -y \
      ncurses-devel bison flex openssl-devel elfutils-libelf-devel \
      bc dwarves git fakeroot rsync cpio kmod ccache \
      zstd elfutils-devel gawk
    $USE_CLANG && run_sudo dnf install -y clang lld llvm
    ;;
  pacman)
    run_sudo pacman -Sy --needed --noconfirm \
      base-devel ncurses bison flex openssl libelf \
      bc dwarves git fakeroot rsync cpio kmod ccache \
      zstd elfutils gawk
    $USE_CLANG && run_sudo pacman -S --needed --noconfirm clang lld llvm
    ;;
esac

### ---------- 4. Enable ccache -----------------------------------------------
log "Configuring ccache"
export PATH="/usr/lib/ccache:$PATH"
ccache --max-size=10G >/dev/null 2>&1 || true
ccache -z >/dev/null 2>&1 || true

### ---------- 5. Get the latest stable kernel version ------------------------
log "Querying kernel.org for the latest stable release"

CURL_OPTS=(--fail --silent --show-error --location --connect-timeout 10 --max-time 30 --retry 2)

KVER=""

# Primary method: parse releases.json (fast if it works)
JSON=$(curl "${CURL_OPTS[@]}" https://www.kernel.org/releases.json || true)
if [[ -n "$JSON" ]]; then
  KVER=$(echo "$JSON" | grep -oP '"version":\s*"\K[^"]+(?=".*?"moniker":\s*"stable")' 2>/dev/null | head -n1 || true)
fi

# Fallback: git ls-remote against the stable tree (no JSON parsing, very reliable)
if [[ -z "$KVER" ]]; then
  warn "releases.json parsing failed or timed out, falling back to git ls-remote"
  KVER=$(git ls-remote --tags --refs https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git 2>/dev/null \
    | awk -F'refs/tags/v' '{print $2}' \
    | grep -E '^[0-9]+\.[0-9]+(\.[0-9]+)?$' \
    | sort -V | tail -n1 || true)
fi

if [[ -z "$KVER" ]]; then
  err "Could not determine latest stable kernel version. Check your internet connection:
    curl -v --max-time 15 https://www.kernel.org/releases.json
  If that also hangs/fails, your network may be blocking kernel.org or git.kernel.org."
fi
log "Latest stable kernel: $KVER"

SRC_DIR="$HOME/kernel-build"
mkdir -p "$SRC_DIR"
LAST_VER_FILE="$SRC_DIR/.last_built_version${USE_CLANG:+-clang}"

if [[ -f "$LAST_VER_FILE" ]]; then
  LAST_VER=$(cat "$LAST_VER_FILE")
  if [[ "$LAST_VER" == "$KVER" ]] && ! $FORCE; then
    echo
    echo "No update: $KVER is the same version you last built with this script"
    echo "(recorded on $(date -r "$LAST_VER_FILE" 2>/dev/null || echo 'unknown date'))."
    echo "Currently running kernel: $(uname -r)"
    echo "Nothing to do. Re-run with --force if you want to rebuild it anyway"
    echo "(e.g. to pick up a config or toolchain-flag change)."
    exit 0
  fi
fi

MAJOR=$(echo "$KVER" | cut -d. -f1)
cd "$SRC_DIR"

TARBALL="linux-${KVER}.tar.xz"
URL="https://cdn.kernel.org/pub/linux/kernel/v${MAJOR}.x/${TARBALL}"

if [[ ! -f "$TARBALL" ]]; then
  log "Downloading $URL"
  curl --fail --location --connect-timeout 10 --max-time 1800 --retry 3 -o "$TARBALL" "$URL"
else
  log "$TARBALL already downloaded, skipping"
fi

log "Extracting source"
rm -rf "linux-${KVER}"
tar xf "$TARBALL"
cd "linux-${KVER}"

# Drop the companion install script into the source dir so it's already
# in the right place (next to the Makefile) once the build finishes.
if [[ -f "$SCRIPT_DIR/install-custom-kernel.sh" ]]; then
  cp "$SCRIPT_DIR/install-custom-kernel.sh" .
  chmod +x install-custom-kernel.sh
else
  warn "install-custom-kernel.sh not found next to this script — skipping copy."
fi

### ---------- 6. Configure the kernel ----------------------------------------
log "Generating base config from your currently running kernel"

if [[ -f "/boot/config-$(uname -r)" ]]; then
  cp "/boot/config-$(uname -r)" .config
else
  warn "No existing /boot/config-$(uname -r) found; using defconfig instead."
  make defconfig
fi

# Update config for the new kernel version, keeping your existing choices
make olddefconfig

# Trim to only the modules you actually use right now -> much faster builds.
# (Comment this line out if you want a fully generic kernel instead.)
# NOTE: `yes` gets killed by SIGPIPE once `make` stops reading, which under
# `set -o pipefail` makes the pipeline look like it failed even though the
# config step succeeded. The `|| true` guards against that false failure.
yes "" | make localmodconfig || true

# localmodconfig only keeps modules that are currently loaded, which means
# it silently drops USB mass-storage support if no USB drive happened to be
# plugged in while this script ran. Force these back on regardless, since
# losing USB drive support is the kind of thing you only notice much later.
scripts/config --module CONFIG_USB_STORAGE 2>/dev/null || true
scripts/config --module CONFIG_USB_UAS 2>/dev/null || true
scripts/config --enable CONFIG_SCSI 2>/dev/null || true
scripts/config --enable CONFIG_BLK_DEV_SD 2>/dev/null || true

# Ubuntu/Debian kernels point CONFIG_SYSTEM_TRUSTED_KEYS and
# CONFIG_SYSTEM_REVOCATION_KEYS at debian/canonical-*.pem files that only
# exist in Ubuntu's own kernel source package, not in this vanilla
# kernel.org tarball. Left as-is, the certs build step fails looking for a
# file that was never downloaded. Clear them so the kernel generates and
# uses its own build-time keys instead.
scripts/config --set-str CONFIG_SYSTEM_TRUSTED_KEYS "" 2>/dev/null || true
scripts/config --set-str CONFIG_SYSTEM_REVOCATION_KEYS "" 2>/dev/null || true

if $FULL_DEBUG_INFO; then
  log "Keeping full debug info (--full-debug-info was passed)"
else
  log "Trimming debug info to reduce peak RAM use during the final link step"
  # The final vmlinux link is a single large process that has to hold the
  # whole symbol table / debug info in memory at once. On low-RAM machines
  # this is a common cause of the linker getting OOM-killed (Error 137).
  # Stripping debug info significantly cuts that peak memory usage.
  scripts/config --disable CONFIG_DEBUG_INFO 2>/dev/null || true
  scripts/config --set-val CONFIG_DEBUG_INFO_NONE y 2>/dev/null || true
  scripts/config --disable CONFIG_DEBUG_INFO_DWARF4 2>/dev/null || true
  scripts/config --disable CONFIG_DEBUG_INFO_DWARF5 2>/dev/null || true
  scripts/config --disable CONFIG_DEBUG_INFO_BTF 2>/dev/null || true
  scripts/config --disable CONFIG_DEBUG_INFO_BTF_MODULES 2>/dev/null || true
fi

make olddefconfig

### ---------- 7. Set up optimized toolchain flags ----------------------------
log "Setting optimization flags"

COMMON_FLAGS="-march=${MARCH} -mtune=${MARCH} -O2"
MAKE_ARGS=( -j"${JOBS}" LOCALVERSION="${LOCALVERSION}" )

if $USE_CLANG; then
  log "Using Clang + LLD build"
  MAKE_ARGS+=( LLVM=1 LLVM_IAS=1 )
  if $USE_LTO; then
    log "Enabling ThinLTO (--lto was passed)"
    scripts/config --enable CONFIG_LTO_CLANG_THIN 2>/dev/null || true
    scripts/config --disable CONFIG_LTO_NONE 2>/dev/null || true
    make olddefconfig
  fi
else
  log "Using GCC with ccache and CPU-specific flags"
  export KCFLAGS="${COMMON_FLAGS}"
  export CC="ccache gcc"
fi

### ---------- 8. Build --------------------------------------------------------
log "Building kernel ${KVER}${LOCALVERSION} with ${JOBS} parallel jobs (this will take a while)"

BUILD_LOG="$SRC_DIR/build-${KVER}${LOCALVERSION}.log"

time make "${MAKE_ARGS[@]}" 2>&1 | tee "$BUILD_LOG"

log "Building kernel modules"
time make "${MAKE_ARGS[@]}" modules 2>&1 | tee -a "$BUILD_LOG"

### ---------- 9. Ready to install --------------------------------------------
log "Build complete!"
echo "Kernel ${KVER}${LOCALVERSION} is built. Install it by running:"
echo "    cd $(pwd)"
echo "    ./install-custom-kernel.sh"
echo
ccache -s 2>/dev/null || true

# Record this version so future runs can tell you when there's nothing new
# to build (see --force to override).
echo "$KVER" > "$LAST_VER_FILE"
