# Kernel Manager

Kernel Manager is a Linux desktop application for building, installing and managing
custom Linux kernels. Its Python and Tkinter interface combines kernel builds,
installed-kernel management, GRUB tools and memory controls in an Ubuntu-themed UI.

Copyright (C) 2026 Adrian

## Features and recent changes

- View installed kernels, identify the running kernel and remove surplus kernels.
- Check kernel.org for the latest stable release automatically at startup or manually.
- Build with GCC or Clang, optional Clang ThinLTO and full debug information.
- Choose automatic build parallelism based on usable CPUs and available RAM, or set
  a manual job count. ThinLTO backend threads follow the selected job count.
- Scan hardware and use Hardware Optimised settings that retain common Linux
  platform and installed-software support while pruning irrelevant drivers.
- Follow live build output, inspect saved logs and clean up build source directories.
- Install completed builds and select a GRUB default or one-time boot entry.
- Configure GRUB menu visibility, timeout, display resolution and graphics handoff.
- Monitor RAM, swap devices and swappiness; run read-only swap assessments with
  conditional recommendations and memory-pressure information.
- Change swappiness or enable/disable individual existing swap devices through an
  optional installed helper, with confirmation, authentication and safety checks.
- Scroll nested panels and dialogs using the mouse wheel at the pointer location.
- Generate and enrol Secure Boot keys, sign kernels/modules, check dependencies
  and install a desktop launcher.

Swap-size adjustment has been removed. Assessments remain read-only, and the
application does not create, format or resize swap storage.

## Requirements

- Linux with Python 3, Tkinter and a graphical desktop session.
- Bash, sudo and the build tools required by your selected toolchain.
- Internet access for kernel update checks, source downloads and missing packages.
- Sufficient RAM and disk space for a kernel build; the build script recommends
  15–25 GB of free disk space.

Dependency installation supports apt, dnf and pacman. Automatic kernel installation
requires `installkernel`, `update-initramfs` or `dracut`, and a supported existing
GRUB configuration. Boot Loader Specification (BLS) and other unsupported boot
layouts require manual kernel installation. GRUB menu settings require
`/etc/default/grub`, `/boot/grub/grub.cfg` and `update-grub`. Display settings also
support the documented `/boot/grub2/grub.cfg` layout with `grub2-mkconfig`.

## Getting started

Keep the repository files together and run these commands from its directory.
On Debian or Ubuntu, install the GUI runtime if needed:

```sh
sudo apt install python3 python3-tk
```

Launch Kernel Manager as your normal user:

```sh
python3 kernel-manager-gui.py
```

The application checks dependencies on first launch and offers to install missing
packages. Privileged actions request sudo authentication.

To add it to your application menu, use **Tools → Install / Update Desktop
Launcher**, or run:

```sh
bash install-desktop-entry.sh
```

The launcher references the repository location. Update it if you move the files.

## Building and installing a kernel

1. Open **Build New Kernel** and select your build mode and toolchain options.
2. Leave **Automatic** checked to choose jobs from current CPU/memory resources,
   or uncheck it and set **Manual jobs**.
3. Start the build and follow its live output. Use **Build Logs** for saved logs.
4. After a successful build, choose **Install Now** to install that kernel.

Automatic jobs are estimated in the GUI and checked again by the script immediately
before compilation. The count stays fixed during the build. ThinLTO and debug builds
receive larger memory allowances; swap is not counted as build memory. These are
conservative estimates, not a guarantee against exhausting RAM. Manual counts are
honoured without automatic clamping. See [Automatic build jobs](docs/automatic-build-jobs.md).

You can also build from a terminal:

```sh
# Default GCC build with automatic jobs
./build-custom-kernel.sh

# Clang with ThinLTO
./build-custom-kernel.sh --clang --lto

# Manual jobs and release suffix
./build-custom-kernel.sh --jobs 4 --localversion -custom

# All available options
./build-custom-kernel.sh --help
```

Builds are stored under `~/kernel-build`. Building and installing are separate:
after a command-line build, follow the printed instructions to enter the completed
source directory and run `./install-custom-kernel.sh` there. Run both scripts as
your normal user; they invoke sudo when needed.

Keep a working fallback kernel and verify your boot menu before rebooting. The
installer refuses to overwrite an existing kernel release. Secure Boot requires
appropriate kernel/module signatures and an enrolled key before booting.

Hardware Optimised mode retains common container/firewall, VPN, filesystem,
IPC/security and peripheral support, including currently unused features. It also
checks installed software with known kernel requirements. Third-party, out-of-tree
or undocumented dependencies still need review.

## GRUB tools

Use **Installed Kernels** to inspect actual GRUB entries and choose a default or
one-time boot entry. The **Tools** tab provides two additional controls:

- **GRUB Boot Menu:** refresh the current settings, choose Hidden, Show for
  5 seconds or Show for 15 seconds, then **Apply GRUB Setting**. Review the diff
  and confirm before authentication.
- **GRUB Display Resolution:** choose `auto`, `1024x768`, `1280x720` or `1920x1080`
  and optionally keep GRUB graphics mode for Linux. Use **Apply Display Settings
  and Regenerate GRUB…**, review the preview and confirm. Safe defaults are `auto`
  with keep unchecked; explicit resolutions include an `auto` fallback.

Selecting options or refreshing does not apply changes. Supported transactions
back up the defaults and generated menu and attempt restoration on failure.
Conflicting or unsupported configurations are refused. Display settings show
configured values; they cannot prove firmware support. Check available modes with
`videoinfo` at the GRUB prompt. Neither settings tool reboots the machine.

See [GRUB boot menu settings](docs/grub-menu-settings.md) and
[GRUB display resolution](docs/grub-display-resolution.md) for layout support and
recovery instructions.

## Memory monitoring and controls

Open **Memory Management** and choose **Refresh Memory** to view RAM total/available,
swap total/free, device types, usage, priorities and current `vm.swappiness`.
Snapshots describe the refresh time; observed configuration does not prove boot
activation or complete coverage of every swap manager.

**Check Swap Settings** opens a fresh, read-only report covering memory pressure
(PSI when available), swap capacity, disk space, hibernation hints and swappiness.
Recommendations are conditional on representative workload observations. They do
not apply changes or populate a size control. Monitoring and assessment require
neither the privileged helper nor authentication.

To enable changes, review `memory_helper.py` and `install-memory-helper.sh`, then
choose **Helper Setup → Install Helper**. The dialog runs setup with sudo
authentication, displays progress in the operation log and refreshes memory
readiness afterwards. You can also run setup from the repository directory:

```sh
sudo bash install-memory-helper.sh
```

Setup installs a separate root-owned helper and private state directory. It does
not change memory settings or add passwordless sudo permissions. Choose **Refresh
Memory** afterwards. Repeat setup when the helper source changes; the GUI checks
that the installed copy matches the checkout.

- **Swappiness:** enter an integer from 0 to 200, choose **Temporary (until reboot)**
  or **Persistent (future boots)**, then **Apply Swappiness** and confirm. Persistent
  changes use a dedicated sysctl file and refuse conflicting definitions.
- **Restore Previous Swappiness:** confirm restoration of the recorded previous
  setting from this boot. This restores a recorded value, not a factory default;
  external changes or backups from an earlier boot require administrator review.
- **Swap on/off:** select an existing device and choose **Enable Selected** or
  **Disable Selected**. Changes affect runtime state only. Enable supports trusted
  configured swap, initialized zram or unchanged devices disabled by this helper.
  Disable requires sufficient RAM headroom and acceptable hibernation checks.
- **Add Device to List…:** enter an absolute device path to rediscover a configured
  or previously disabled device. This only adds monitoring; it grants no permissions.

Every change requires confirmation and sudo authentication; unsafe requests are
refused. Other system managers may override runtime swap state. See
[Memory Management](docs/memory-management.md) for eligibility, recovery and limits.

## Interface and scrolling

The interface uses Ubuntu-style colours and shared widget styles. Mouse-wheel
scrolling follows the pointer across panels and later-opened dialogs, reaches
nested content and passes to an enclosing panel at an edge. Value controls such
as combo boxes and sliders retain their native behaviour.

## Validation

Run the regression suite from the repository directory:

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary fixtures and mocked privileged commands. GUI tests need a
working display; some native kernel checks need additional source/toolchain
fixtures and otherwise skip. Passing fixture tests does not replace testing
privileged operations in a disposable system.

## Operational improvements

The repository keeps structured machine-readable operational data in
`~/.cache/kernel-manager/`. Build state is stored in `build-metadata.json`, while
command and notification events are stored in `operations.jsonl`. The shared GRUB
entry parser now lives with the GRUB transaction helpers rather than in the GUI
controller. Long-running operation dialogs show an activity indicator and a
clear completion or failure status.

CI runs the regression suite on Ubuntu, checks Python compilation and shell
syntax, runs Ruff's critical syntax/undefined-name rules and runs ShellCheck at
error severity. Fedora and Arch container smoke jobs also exercise the dependency
catalog and shell/Python checks. Dependency
names intentionally follow each distribution's repositories rather than pinning
package versions; repeatable kernel builds are instead tied to the verified
kernel.org source archive and recorded build metadata. GRUB changes already use
backups and automatic rollback on failed transactions, and memory changes use a
durable recovery record with explicit restore safeguards; see the related
documentation for recovery limits.

## Further documentation

- [Memory Management](docs/memory-management.md)
- [Automatic build jobs](docs/automatic-build-jobs.md)
- [Desktop launcher integration](docs/desktop-launcher.md)
- [GRUB boot menu settings](docs/grub-menu-settings.md)
- [GRUB display resolution](docs/grub-display-resolution.md)
- [Software compatibility architecture](docs/software-compatibility.md)
- [Hardware compatibility validation](docs/hardware-compatibility-validation.md)
- [Sandy Bridge configuration diagnosis](docs/sandy-bridge-config-validation.md)
- [Container kernel diagnosis](docs/container-kernel-diagnosis.md)

## License

Kernel Manager is free and open-source software licensed under the GNU General
Public License version 3 (GPLv3). You may use, study, modify and redistribute it
under those terms. Distributed modified versions must remain under GPLv3 and
provide corresponding source code as required by the licence.

See [LICENSE](LICENSE) for the complete terms.
