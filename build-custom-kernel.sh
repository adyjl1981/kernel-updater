#!/usr/bin/env bash
#
# build-custom-kernel.sh
#
# Detects your hardware, installs the packages needed to build a kernel,
# downloads the latest STABLE mainline Linux kernel source from kernel.org,
# and builds it with a performance-tuned toolchain (CPU-specific flags,
# optional LTO, optional Clang/LLD, ccache).
#
# Dependency targets: Debian/Ubuntu (apt), Fedora/RHEL (dnf), Arch (pacman)
#
# USAGE:
#   chmod +x build-custom-kernel.sh
#   ./build-custom-kernel.sh              # GCC build, -march=native, ccache
#   ./build-custom-kernel.sh --clang --lto  # Clang+LLD with ThinLTO
#   ./build-custom-kernel.sh --jobs 8     # override parallel job count
#
# IMPORTANT SAFETY NOTES:
#   - The installer refuses an existing release to avoid overwriting it.
#     Verify your boot menu and a working fallback before rebooting.
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
HARDWARE_OPTIMISED=false
FORCE=false
FULL_DEBUG_INFO=false
JOBS=""
LOCALVERSION=""
LOCALVERSION_EXPLICIT=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --clang) USE_CLANG=true; shift ;;
    --lto) USE_LTO=true; shift ;;
    --force) FORCE=true; shift ;;
    --full-debug-info) FULL_DEBUG_INFO=true; shift ;;
    --hardware-optimised) HARDWARE_OPTIMISED=true; shift ;;
    --jobs) [[ $# -ge 2 && $2 =~ ^[1-9][0-9]*$ ]] || { echo "--jobs requires a positive integer" >&2; exit 1; }; JOBS="$2"; shift 2 ;;
    --localversion) [[ $# -ge 2 && $2 =~ ^-[a-zA-Z0-9._+-]+$ ]] || { echo "--localversion requires a suffix such as -custom" >&2; exit 1; }; LOCALVERSION="$2"; LOCALVERSION_EXPLICIT=true; shift 2 ;;
    -h|--help)
      echo "Usage: $0 [--clang] [--lto] [--hardware-optimised] [--force] [--full-debug-info] [--jobs N] [--localversion -mytag]"
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

if ! $LOCALVERSION_EXPLICIT; then
  if $HARDWARE_OPTIMISED; then
    LOCALVERSION="-optimized"
  else
    LOCALVERSION="-custom"
  fi
fi

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
    sudo -A --preserve-env=DEBIAN_FRONTEND,NEEDRESTART_MODE "$@"
  else
    sudo --preserve-env=DEBIAN_FRONTEND,NEEDRESTART_MODE "$@"
  fi
}

[[ $HOME != *$'\n'* ]] || err "Home directories containing newlines are not supported."

[[ $EUID -eq 0 ]] && err "Run this as a normal user (it will sudo when needed), not as root."

if $USE_LTO && ! $USE_CLANG; then
  err "--lto requires --clang (this script only wires up Clang ThinLTO). Add --clang too."
fi

# Prevent apt/dpkg/needrestart from popping up interactive dialogs (e.g. the
# "which services should be restarted?" whiptail prompt) during package
# installs, so the script never stops waiting for a keypress.
export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a

# Serialize builds/installations, including operations started by the GUI.
SRC_DIR="$HOME/kernel-build"
mkdir -p "$SRC_DIR"
if [[ ${KERNEL_MANAGER_LOCK_FD:-} =~ ^[0-9]+$ ]]; then
  exec 9>&"$KERNEL_MANAGER_LOCK_FD"
else
  exec 9>"$SRC_DIR/.operation.lock"
fi
flock -n 9 || err "Another kernel operation is running."

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

CPU_MODEL=$(awk -F: '/^(model name|Hardware|Processor)[[:space:]]*:/ {sub(/^ +/, "", $2); print $2; exit}' /proc/cpuinfo)
CPU_VENDOR=$(awk -F: '/^vendor_id[[:space:]]*:/ {sub(/^ +/, "", $2); print $2; exit}' /proc/cpuinfo)
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
  warn "Consider lowering --jobs if you hit OOM / swap thrashing (e.g. --jobs $((NPROC > 1 ? NPROC/2 : 1)))."
fi

# Native tuning is supported here on x86; keep portable defaults elsewhere.
COMMON_FLAGS="-O2"
if [[ "$ARCH" == "x86_64" ]]; then
  COMMON_FLAGS+=" -march=native -mtune=native"
fi

# Secure Boot check
if command -v mokutil >/dev/null 2>&1 && mokutil --sb-state 2>/dev/null | grep -qi "enabled"; then
  warn "Secure Boot is ENABLED. An unsigned custom kernel may refuse to boot."
  warn "You'll need to either disable Secure Boot, or sign the kernel/modules yourself (MOK enrollment) after the build."
fi

### ---------- 3. Verify build dependencies ----------------------------------
log "Verifying build dependencies (installation requires explicit approval in Kernel Manager)"
REQUIRED_TOOLS=(gcc g++ make perl bison flex bc git fakeroot rsync cpio depmod ccache curl pahole zstd gawk python3 gpg xz tar tee flock grep awk find df nproc dirname)
$USE_CLANG && REQUIRED_TOOLS+=(clang ld.lld llvm-ar)
MISSING_TOOLS=()
for tool in "${REQUIRED_TOOLS[@]}"; do
  command -v "$tool" >/dev/null 2>&1 || MISSING_TOOLS+=("$tool")
done
(( ${#MISSING_TOOLS[@]} == 0 )) || err "Missing build tools: ${MISSING_TOOLS[*]}. Use Tools > Check Dependencies in Kernel Manager, or install them with your distribution package manager."

# From here onward there are no privileged package operations to interrupt.
echo "KERNEL_MANAGER_CANCELLABLE=1"

### ---------- 4. Enable ccache -----------------------------------------------
log "Configuring ccache"
ccache --max-size=10G >/dev/null 2>&1 || true
ccache -z >/dev/null 2>&1 || true

### ---------- 5. Get the latest stable kernel version ------------------------
log "Querying kernel.org for the latest stable release"

CURL_OPTS=(--fail --silent --show-error --location --connect-timeout 10 --max-time 30 --retry 2)

# Python's JSON parser is also used by the GUI; never parse JSON with grep.
JSON=$(curl "${CURL_OPTS[@]}" https://www.kernel.org/releases.json || true)
KVER=$(printf '%s' "$JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["latest_stable"]["version"])' 2>/dev/null || true)
if [[ ! "$KVER" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]]; then
  warn "Release lookup failed; trying stable Git tags"
  KVER=$(timeout 45 git ls-remote --tags --refs https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git 2>/dev/null \
    | awk -F'refs/tags/v' '{print $2}' | grep -E '^[0-9]+\.[0-9]+(\.[0-9]+)?$' | sort -V | tail -n1 || true)
fi
[[ "$KVER" =~ ^[0-9]+\.[0-9]+(\.[0-9]+)?$ ]] || err "Could not determine the stable release. Check your internet connection."
log "Latest stable kernel: $KVER"

TOOLCHAIN=gcc
$USE_CLANG && TOOLCHAIN=clang
BUILD_ID="$KVER $TOOLCHAIN $USE_LTO $FULL_DEBUG_INFO $HARDWARE_OPTIMISED $LOCALVERSION"
TREE="$SRC_DIR/linux-$KVER"
if ! $FORCE; then
  for candidate in "$SRC_DIR"/linux-"$KVER"*; do
    if [[ -f "$candidate/.kernel-manager-complete" && -s "$candidate/vmlinux" && -s "$candidate/.kernel-manager-make-args" ]]; then
      if [[ $(cat "$candidate/.kernel-manager-complete") == "$BUILD_ID" ]] && (cd "$candidate" && sha256sum --status -c .kernel-manager-checksums); then
        log "This build is already complete."
        printf 'KERNEL_MANAGER_BUILD_DIR=%s\n' "$candidate"
        exit 0
      fi
    fi
  done
fi

MAJOR=$(echo "$KVER" | cut -d. -f1)
cd "$SRC_DIR"

TARBALL="linux-${KVER}.tar.xz"
URL="https://cdn.kernel.org/pub/linux/kernel/v${MAJOR}.x/${TARBALL}"

if [[ ! -f "$TARBALL" ]]; then
  log "Downloading $URL"
  curl --fail --location --connect-timeout 10 --max-time 1800 --retry 3 -o "$TARBALL.part" "$URL"
  mv -- "$TARBALL.part" "$TARBALL"
fi

# Verify the uncompressed tar against kernel.org's release-signing key.  Since
# Linux 4.18 kernel.org tarballs are signed by Greg Kroah-Hartman.  Both the WKD
# address and full fingerprint are published at https://www.kernel.org/signature.html.
# Do not infer trust from a key ID in the downloaded signature.
SIGNER=647F28654894E3BD457199BE38DBBDC86092693E
SIGNER_WKD=gregkh@kernel.org
GPG_DIR="$SRC_DIR/.gnupg"
mkdir -p "$GPG_DIR"
chmod 700 "$GPG_DIR"
curl "${CURL_OPTS[@]}" -o "$TARBALL.sign.part" "${URL%.xz}.sign"
mv -- "$TARBALL.sign.part" "$TARBALL.sign"

keyring_has_expected_signer() {
  gpg --homedir "$GPG_DIR" --batch --with-colons --fingerprint --list-keys "$SIGNER" 2>/dev/null |
    awk -F: -v key="$SIGNER" '$1 == "fpr" && $10 == key {found=1} END {exit !found}'
}

acquire_expected_signer() {
  local staging fetched
  staging=$(mktemp -d "$SRC_DIR/.gnupg-fetch.XXXXXX") || return 1
  chmod 700 "$staging"
  log "Kernel.org release signing key is missing; retrieving $SIGNER_WKD through kernel.org WKD"
  echo "KERNEL_MANAGER_STATUS=Authenticating the kernel.org release signing key…"
  if ! timeout 60 gpg --homedir "$staging" --batch --auto-key-locate clear,wkd --locate-keys "$SIGNER_WKD" >/dev/null 2>&1; then
    rm -rf -- "$staging"
    return 1
  fi
  fetched=$(gpg --homedir "$staging" --batch --with-colons --fingerprint --list-keys "$SIGNER_WKD" 2>/dev/null |
    awk -F: '$1 == "fpr" {print $10; exit}')
  if [[ "$fetched" != "$SIGNER" ]]; then
    warn "Kernel.org WKD returned unexpected fingerprint ${fetched:-<none>}; expected $SIGNER. The key was not trusted or imported."
    rm -rf -- "$staging"
    return 2
  fi
  if ! gpg --homedir "$staging" --batch --export "$SIGNER" |
       gpg --homedir "$GPG_DIR" --batch --import >/dev/null 2>&1; then
    rm -rf -- "$staging"
    return 1
  fi
  rm -rf -- "$staging"
  keyring_has_expected_signer
}

verify_source_signature() {
  xz -cd -- "$TARBALL" |
    gpg --homedir "$GPG_DIR" --batch --status-fd=1 --verify "$TARBALL.sign" - 2>&1
}

echo "KERNEL_MANAGER_STATUS=Cryptographically verifying the downloaded kernel source…"
VERIFY_STATUS=""
if ! VERIFY_STATUS=$(verify_source_signature); then
  if printf '%s\n' "$VERIFY_STATUS" | grep -q '^\[GNUPG:\] NO_PUBKEY '; then
    acquire_rc=0
    acquire_expected_signer || acquire_rc=$?
    if (( acquire_rc == 2 )); then
      err "Refusing an untrusted kernel.org signing key. Source archive retained at $SRC_DIR/$TARBALL"
    elif (( acquire_rc != 0 )); then
      err "The required kernel.org signing key could not be obtained and validated safely. Check HTTPS/network access to kernel.org; source archive retained at $SRC_DIR/$TARBALL"
    fi
    log "Trusted kernel.org release signing key acquired; retrying source verification"
    echo "KERNEL_MANAGER_STATUS=Trusted signing key acquired; retrying source verification…"
    if ! VERIFY_STATUS=$(verify_source_signature); then
      if printf '%s\n' "$VERIFY_STATUS" | grep -q '^\[GNUPG:\] NO_PUBKEY '; then
        err "The signature requires a public key that is not an approved kernel.org release key. Source archive retained at $SRC_DIR/$TARBALL"
      fi
      err "Kernel source has a bad or invalid signature and may be corrupt or tampered with. Source archive retained at $SRC_DIR/$TARBALL"
    fi
  else
    err "Kernel source has a bad or invalid signature and may be corrupt or tampered with. Source archive retained at $SRC_DIR/$TARBALL"
  fi
fi
printf '%s\n' "$VERIFY_STATUS" | awk -v key="$SIGNER" '$2 == "VALIDSIG" && ($3 == key || $NF == key) {ok=1} END {exit !ok}' || \
  err "The source signature is valid but was made by an unexpected key. Source archive retained at $SRC_DIR/$TARBALL"
log "Kernel source signature is valid and matches the trusted kernel.org release signer"
echo "KERNEL_MANAGER_STATUS=Kernel source signature verified."

# Leave existing trees in place: installed modules may link to their headers.
if [[ -e "$TREE" || -L "$TREE" ]]; then
  TREE=$(mktemp -d "$SRC_DIR/linux-$KVER.rebuild.XXXXXX")
  log "Existing source tree kept in place; using $TREE"
else
  mkdir "$TREE"
fi
log "Extracting verified source"
tar --no-same-owner --strip-components=1 -xf "$TARBALL" -C "$TREE"
cd "$TREE"

# Drop the companion install script into the source dir so it's already
# in the right place (next to the Makefile) once the build finishes.
if [[ -f "$SCRIPT_DIR/install-custom-kernel.sh" ]]; then
  cp "$SCRIPT_DIR/install-custom-kernel.sh" .
  chmod +x install-custom-kernel.sh
else
  warn "install-custom-kernel.sh not found next to this script — skipping copy."
fi

# Keep every make invocation on the same compiler, flags, and release.
MAKE_ARGS=( LOCALVERSION="$LOCALVERSION" KCFLAGS="$COMMON_FLAGS" )
if $USE_CLANG; then
  MAKE_ARGS+=( LLVM=1 LLVM_IAS=1 CC="ccache clang" )
else
  MAKE_ARGS+=( CC="ccache gcc" )
fi

### ---------- 6. Configure the kernel ----------------------------------------
log "Generating base config from your currently running kernel"

if [[ -f "/boot/config-$(uname -r)" ]]; then
  cp "/boot/config-$(uname -r)" .config
else
  $HARDWARE_OPTIMISED && err "The running kernel configuration is unavailable; Hardware Optimised mode cannot safely continue. Use Standard mode."
  warn "No existing /boot/config-$(uname -r) found; using defconfig instead."
  make "${MAKE_ARGS[@]}" defconfig
fi

# Update config for the new kernel version, keeping your existing choices
make "${MAKE_ARGS[@]}" olddefconfig

# Keep the running kernel's module coverage; loaded modules alone do not
# describe all hardware or filesystems needed at the next boot.
if $HARDWARE_OPTIMISED; then
  [[ -r "$SCRIPT_DIR/hardware_optimizer.py" ]] || err "Hardware scanner is missing. Use Standard mode."
  log "Scanning hardware and validating boot requirements"
  HARDWARE_REPORT="$PWD/.kernel-manager-hardware.json"
  python3 "$SCRIPT_DIR/hardware_optimizer.py" scan --save-report "$HARDWARE_REPORT" || err "Hardware scan failed. Use Standard mode."
  OPTIMISED_LSMOD=$(mktemp)
  trap 'rm -f -- "${OPTIMISED_LSMOD:-}"' EXIT
  python3 "$SCRIPT_DIR/hardware_optimizer.py" lsmod --report "$HARDWARE_REPORT" > "$OPTIMISED_LSMOD" || err "Hardware scan is incomplete. Use Standard mode."
  # localmodconfig removes non-module early-boot bools.  Retain a read-only
  # snapshot of the known-working config so the optimiser can restore them.
  BASELINE_CONFIG="$PWD/.kernel-manager-working-config"
  cp .config "$BASELINE_CONFIG"
  log "Optimising configuration for detected and active drivers"
  # The localmodconfig make target ends with interactive conf --oldconfig.
  # Run its pruning step directly, then let native Kconfig assign NEW defaults
  # without ever consulting stdin. Do not pipe yes into a pipefail pipeline.
  KCONFIG_SRCARCH="$ARCH"
  case "$ARCH" in
    x86_64|i?86) KCONFIG_SRCARCH=x86 ;;
    aarch64) KCONFIG_SRCARCH=arm64 ;;
    arm*) KCONFIG_SRCARCH=arm ;;
    ppc*) KCONFIG_SRCARCH=powerpc ;;
    s390x) KCONFIG_SRCARCH=s390 ;;
    riscv*) KCONFIG_SRCARCH=riscv ;;
  esac
  SRCARCH="$KCONFIG_SRCARCH" srctree=. objtree=. LSMOD="$OPTIMISED_LSMOD" \
    perl scripts/kconfig/streamline_config.pl --localmodconfig . Kconfig > .kernel-manager-local.config
  mv .kernel-manager-local.config .config
  make "${MAKE_ARGS[@]}" olddefconfig < /dev/null
  # Always restore the broad peripheral safety set after localmodconfig.
  python3 "$SCRIPT_DIR/hardware_optimizer.py" apply .config --baseline "$BASELINE_CONFIG" --source "$PWD" --report "$HARDWARE_REPORT" || err "Could not apply the compatibility safety set. Use Standard mode."
  make "${MAKE_ARGS[@]}" olddefconfig
  python3 "$SCRIPT_DIR/hardware_optimizer.py" verify .config --baseline "$BASELINE_CONFIG" --source "$PWD" --report "$HARDWARE_REPORT" || err "Boot/network/container settings did not survive configuration validation. Use Standard mode."
fi

# Ubuntu/Debian kernels point CONFIG_SYSTEM_TRUSTED_KEYS and
# CONFIG_SYSTEM_REVOCATION_KEYS at debian/canonical-*.pem files that only
# exist in Ubuntu's own kernel source package, not in this vanilla
# kernel.org tarball. Left as-is, the certs build step fails looking for a
# file that was never downloaded. Clear them so the kernel generates and
# uses its own build-time keys instead.
scripts/config --set-str CONFIG_SYSTEM_TRUSTED_KEYS ""
scripts/config --set-str CONFIG_SYSTEM_REVOCATION_KEYS ""

if $FULL_DEBUG_INFO; then
  log "Keeping full debug info (--full-debug-info was passed)"
else
  log "Trimming debug info to reduce peak RAM use during the final link step"
  # The final vmlinux link is a single large process that has to hold the
  # whole symbol table / debug info in memory at once. On low-RAM machines
  # this is a common cause of the linker getting OOM-killed (Error 137).
  # Stripping debug info significantly cuts that peak memory usage.
  scripts/config --disable CONFIG_DEBUG_INFO
  scripts/config --set-val CONFIG_DEBUG_INFO_NONE y
  scripts/config --disable CONFIG_DEBUG_INFO_DWARF4
  scripts/config --disable CONFIG_DEBUG_INFO_DWARF5
  scripts/config --disable CONFIG_DEBUG_INFO_BTF
  scripts/config --disable CONFIG_DEBUG_INFO_BTF_MODULES
fi

make "${MAKE_ARGS[@]}" olddefconfig

if $USE_LTO; then
  scripts/config --enable CONFIG_LTO_CLANG_THIN
  scripts/config --disable CONFIG_LTO_NONE
  make "${MAKE_ARGS[@]}" olddefconfig
  grep -qx 'CONFIG_LTO_CLANG_THIN=y' .config || err "ThinLTO is not supported by this configuration/toolchain."
fi

# Validate the same inventory after every configuration edit, including LTO.
if $HARDWARE_OPTIMISED; then
  python3 "$SCRIPT_DIR/hardware_optimizer.py" verify .config --baseline "$BASELINE_CONFIG" --source "$PWD" --report "$HARDWARE_REPORT" || err "Final boot/network/container validation failed. Use Standard mode."
fi

# Invalidate completion before compiling and save arguments as data, not shell.
printf '%s\0' "${MAKE_ARGS[@]}" > .kernel-manager-make-args

### ---------- 8. Build --------------------------------------------------------
log "Building kernel ${KVER}${LOCALVERSION} with ${JOBS} parallel jobs (this will take a while)"

BUILD_LOG="$SRC_DIR/build-${KVER}${LOCALVERSION}.log"

time make -j"$JOBS" "${MAKE_ARGS[@]}" 2>&1 | tee "$BUILD_LOG"

### ---------- 9. Ready to install --------------------------------------------
RELEASE=$(make -s "${MAKE_ARGS[@]}" kernelrelease)
[[ "$RELEASE" == "$(cat include/config/kernel.release)" ]] || err "Kernel release changed after the build."
IMAGE=$(make -s "${MAKE_ARGS[@]}" image_name)
[[ -s "$IMAGE" && -s vmlinux && -s System.map ]] || err "Build artifacts are incomplete."
sha256sum .config .kernel-manager-make-args include/config/kernel.release vmlinux System.map "$IMAGE" > .kernel-manager-checksums
printf '%s\n' "$BUILD_ID" > .kernel-manager-complete
log "Build complete!"
printf 'KERNEL_MANAGER_BUILD_DIR=%s\n' "$TREE"
echo "Kernel ${KVER}${LOCALVERSION} is built. Install it by running:"
printf "    cd %q\n" "$PWD"
echo "    ./install-custom-kernel.sh"
echo
ccache -s 2>/dev/null || true
