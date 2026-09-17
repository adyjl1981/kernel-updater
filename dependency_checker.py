"""Dependency discovery and explicitly-approved package installation.

The module has no GUI imports so checks and package transactions can be tested
without changing the host.  Package names mirror the commands used by the
Kernel Manager Python code and its shell helpers.
"""

from dataclasses import dataclass
import shutil
import subprocess


@dataclass(frozen=True)
class Dependency:
    key: str
    name: str
    category: str
    purpose: str
    required: bool
    tools: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    packages: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def packages_for(self, manager):
        return dict(self.packages).get(manager, ())


@dataclass(frozen=True)
class DependencyResult:
    dependency: Dependency
    missing_tools: tuple[str, ...] = ()
    missing_packages: tuple[str, ...] = ()

    @property
    def missing(self):
        return bool(self.missing_tools or self.missing_packages)


DEPENDENCIES = (
    Dependency("runtime", "Core application tools", "Application", "launching scripts, authentication and inspecting files", True,
               ("bash", "sudo", "uname", "du", "sha256sum", "flock", "grep", "awk", "find", "df", "nproc", "dirname"), packages=(("apt", ("bash", "sudo", "coreutils", "util-linux", "grep", "gawk", "findutils")), ("dnf", ("bash", "sudo", "coreutils", "util-linux", "grep", "gawk", "findutils")), ("pacman", ("bash", "sudo", "coreutils", "util-linux", "grep", "gawk", "findutils")))),
    Dependency("build_toolchain", "GCC build toolchain", "Kernel building", "compiling the kernel and host utilities", True,
               ("gcc", "g++", "make", "perl"), packages=(("apt", ("build-essential",)), ("dnf", ("gcc", "gcc-c++", "make", "perl")), ("pacman", ("base-devel",)))),
    Dependency("build_headers", "Kernel build development libraries", "Kernel building", "kernel configuration, ELF and crypto build steps", True,
               ("bison", "flex"), packages=(("apt", ("libncurses-dev", "bison", "flex", "libssl-dev", "libelf-dev", "libudev-dev", "libdw-dev")), ("dnf", ("ncurses-devel", "bison", "flex", "openssl-devel", "elfutils-libelf-devel", "elfutils-devel")), ("pacman", ("ncurses", "bison", "flex", "openssl", "libelf", "elfutils")))),
    Dependency("build_utilities", "Kernel build utilities", "Kernel building", "downloading, verifying, unpacking and building kernel sources", True,
               ("bc", "git", "fakeroot", "rsync", "cpio", "depmod", "ccache", "curl", "pahole", "zstd", "gawk", "python3", "gpg", "xz", "tar", "tee"),
               packages=(("apt", ("bc", "dwarves", "git", "fakeroot", "rsync", "cpio", "kmod", "ccache", "curl", "pahole", "zstd", "gawk", "python3", "gnupg", "xz-utils", "tar", "coreutils")), ("dnf", ("bc", "dwarves", "git", "fakeroot", "rsync", "cpio", "kmod", "ccache", "curl", "zstd", "gawk", "python3", "gnupg2", "xz", "tar", "coreutils")), ("pacman", ("bc", "dwarves", "git", "fakeroot", "rsync", "cpio", "kmod", "ccache", "curl", "zstd", "gawk", "python", "gnupg", "xz", "tar", "coreutils")))),
    Dependency("kernel_install", "Kernel installation tools", "Kernel installation", "installing a built kernel and generating its initramfs", True,
               ("installkernel",), ("update-initramfs", "dracut"), packages=(("apt", ("initramfs-tools", "initramfs-tools-core")), ("dnf", ("dracut",)), ("pacman", ("dracut",)))),
    Dependency("grub", "GRUB tools", "GRUB", "regenerating and selecting GRUB boot entries", True,
               alternatives=("update-grub", "grub-mkconfig", "grub2-mkconfig"), packages=(("apt", ("grub-common",)), ("dnf", ("grub2-tools",)), ("pacman", ("grub",)))),
    Dependency("clang", "Clang/LLVM toolchain", "Optional: Clang/LTO", "building with Clang and ThinLTO", False,
               ("clang", "ld.lld", "llvm-ar"), packages=(("apt", ("clang", "lld", "llvm")), ("dnf", ("clang", "lld", "llvm")), ("pacman", ("clang", "lld", "llvm")))),
    Dependency("signing", "Secure Boot signing tools", "Optional: Secure Boot", "creating/enrolling keys and signing kernels and modules", False,
               ("openssl", "mokutil", "sbsign", "sbverify", "gzip", "xz", "zstd"), packages=(("apt", ("openssl", "mokutil", "sbsigntool", "gzip", "xz-utils", "zstd")), ("dnf", ("openssl", "mokutil", "sbsigntools", "gzip", "xz", "zstd")), ("pacman", ("openssl", "sbsigntools", "gzip", "xz", "zstd")))),
    Dependency("notifications", "Desktop notifications", "Optional: desktop integration", "build and install completion notifications", False,
               ("notify-send",), packages=(("apt", ("libnotify-bin",)), ("dnf", ("libnotify",)), ("pacman", ("libnotify",)))),
    Dependency("terminal", "Terminal emulator", "Optional: Secure Boot", "interactive MOK enrollment", False,
               alternatives=("gnome-terminal", "konsole", "xfce4-terminal", "xterm", "x-terminal-emulator"), packages=(("apt", ("xterm",)), ("dnf", ("xterm",)), ("pacman", ("xterm",)))),
)


def detect_package_manager(which=shutil.which):
    for manager in ("apt", "dnf", "pacman"):
        if which(manager):
            return manager
    return None


def package_is_installed(manager, package, runner=subprocess.run):
    commands = {"apt": ["dpkg-query", "-W", "-f=${db:Status-Status}", package],
                "dnf": ["rpm", "-q", package], "pacman": ["pacman", "-Q", package]}
    command = commands.get(manager)
    if not command:
        return False
    try:
        proc = runner(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return proc.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def check_dependencies(*, dependencies=DEPENDENCIES, which=shutil.which,
                       manager=None, package_check=package_is_installed):
    manager = detect_package_manager(which) if manager is None else manager
    results = []
    for dep in dependencies:
        missing_tools = tuple(tool for tool in dep.tools if not which(tool))
        if dep.alternatives and not any(which(tool) for tool in dep.alternatives):
            missing_tools += (" or ".join(dep.alternatives),)
        packages = dep.packages_for(manager)
        missing_packages = tuple(package for package in packages
                                 if not package_check(manager, package)) if manager else ()
        # A distro package can satisfy a command check; report package names as
        # the actionable result rather than duplicating both columns.
        if missing_packages:
            missing_tools = ()
        results.append(DependencyResult(dep, missing_tools, missing_packages))
    return manager, results


def packages_to_install(results, manager, include_optional=False):
    packages = []
    for result in results:
        if not result.missing or (not result.dependency.required and not include_optional):
            continue
        packages.extend(result.missing_packages or result.dependency.packages_for(manager))
    return list(dict.fromkeys(packages))


def install_packages(manager, packages, env, log_fn, runner):
    """Install an already-approved package list through the GUI's runner."""
    if not packages:
        return
    if manager == "apt":
        runner(["sudo", "-A", "apt-get", "update"], env, log_fn)
        runner(["sudo", "-A", "apt-get", "install", "-y", "--", *packages], env, log_fn)
    elif manager == "dnf":
        runner(["sudo", "-A", "dnf", "install", "-y", *packages], env, log_fn)
    elif manager == "pacman":
        runner(["sudo", "-A", "pacman", "-S", "--needed", "--noconfirm", *packages], env, log_fn)
    else:
        raise RuntimeError("No supported package manager is available for guided installation.")


def initial_check_needed(preferences):
    return preferences.get("dependency_check_completed") is not True

