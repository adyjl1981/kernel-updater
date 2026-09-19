# Kernel Manager

Kernel Manager is a lightweight Linux desktop application for building, installing
and managing custom Linux kernels. Its Python and Tkinter interface brings together
kernel build scripts, installed-kernel management and GRUB boot controls.

Copyright (C) 2026 Adrian

## Features

- View installed kernels, identify the running kernel and remove surplus kernels.
- Check kernel.org for the latest stable kernel release.
- Build with GCC or Clang, optional Clang ThinLTO, configurable parallel jobs and
  optional full debug information.
- Scan hardware and use hardware-optimised build settings.
- Follow live build output, inspect saved logs and clean up build source directories.
- Install completed builds and select a GRUB default or one-time boot entry.
- Configure GRUB menu visibility and timeout on supported systems.
- Generate and enrol Secure Boot keys, and sign kernels and modules.
- Check dependencies and install a desktop launcher.

## Requirements

- Linux with Python 3, Tkinter and a graphical desktop session.
- Bash, sudo and the build tools required by the selected toolchain.
- Internet access to download kernel sources and missing packages.
- Sufficient disk space and memory to compile a Linux kernel; the build script
  recommends 15–25 GB of free disk space.

Dependency installation supports apt, dnf and pacman. Automatic kernel installation
also requires `installkernel`, `update-initramfs` or `dracut`, and a supported
existing GRUB configuration. Boot Loader Specification (BLS) installations and
other unsupported boot layouts require manual kernel installation. The GRUB menu
settings tool specifically requires `/etc/default/grub`, `/boot/grub/grub.cfg`
and `update-grub`.

## Getting started

Keep the repository files together and run these commands from its directory.
On Debian or Ubuntu, install the GUI runtime if needed:

```sh
sudo apt install python3 python3-tk
```

Launch the application as your normal user:

```sh
python3 kernel-manager-gui.py
```

The application checks dependencies on first launch and offers to install missing
packages. Privileged operations request authentication through sudo.

To add Kernel Manager to your application menu, use **Tools → Install / Update
Desktop Launcher**, or run:

```sh
bash install-desktop-entry.sh
```

The launcher references the repository's location. Update it if you move the files.

## Building and installing a kernel

Use the GUI's build controls to select your toolchain and build settings, then
follow the live output. After a successful build, use **Install Now** to install
that kernel.

The build script can also run directly from a terminal:

```sh
# Default GCC build
./build-custom-kernel.sh

# Clang with ThinLTO
./build-custom-kernel.sh --clang --lto

# Set the parallel job count and release suffix
./build-custom-kernel.sh --jobs 4 --localversion -custom

# Show all available options
./build-custom-kernel.sh --help
```

Builds are stored under `~/kernel-build`. Building and installing are separate
steps: after a successful command-line build, follow the printed instructions to
enter the completed source directory and run `./install-custom-kernel.sh` there.
Run both scripts as your normal user; they invoke sudo when needed.

Keep a working fallback kernel and verify your boot menu before rebooting into a
new kernel. The installer refuses to overwrite an existing kernel release. With
Secure Boot enabled, the new kernel and modules need appropriate signatures and
an enrolled key before booting.

## Further documentation

- [Desktop launcher integration](docs/desktop-launcher.md)
- [GRUB boot menu settings](docs/grub-menu-settings.md)
- [Container kernel diagnosis](docs/container-kernel-diagnosis.md)

# License

Kernel Manager is free and open-source software licensed under the GNU General Public License version 3 (GPLv3).

You are free to use, study, modify and redistribute Kernel Manager under the terms of the GPLv3. Modified versions that are distributed must remain available under the GPLv3 and provide the corresponding source code as required by the licence.

See the `LICENSE` file for the complete licence terms.
