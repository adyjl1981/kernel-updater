# Hardware Optimised compatibility model

Hardware Optimised means optimised for this computer's hardware while
conservatively preserving normal Linux userspace/platform capabilities. Booting
successfully is necessary, but does not establish application compatibility.

## Four independently validated inputs

`hardware_optimizer.py` separates:

1. **Boot-critical requirements:** the known-working architecture, firmware,
   initramfs/decompression, console, device mapper/encryption and boot display
   choices in `BOOT_BASELINE_SYMBOLS`, plus explicit root and boot filesystems.
   Their built-in/module choices remain protected; root and boot filesystems are
   built in. A device-mapper root requires built-in device mapper support.
2. **Detected hardware:** saved bound PCI/USB providers in `hardware_devices`,
   with the existing network adapter inventory (including unbound network
   modalias candidates). `hardware_requirements` maps module outputs to target
   Kbuild symbols across drivers and sound. Unknown bound providers fail closed.
   Network-specific transport checks such as ATH9K_PCI remain in place.
3. **General platform baseline:** `COMPATIBILITY_BASELINE`, independent of the
   hardware inventory and software detection. Its named groups are separately
   generated and validated by `compatibility_requirements`.
4. **Installed software:** evidence in `software` maps through
   `SOFTWARE_PROFILES` to named `SOFTWARE_CAPABILITIES`. Old saved reports with
   `container_runtimes` still work. `software_requirements` validates each
   application's profile independently, including the existing extra container
   features selected by the working configuration.

The build still uses native streamline_config.pl to remove irrelevant modules.
It then restores these requirements and runs native `olddefconfig` with closed
stdin. Final validation runs both immediately after restoration and after the
last configuration edit, before compilation. Boolean symbols request `y`;
tristates use existing built-in choices or `m`. Mixed-case symbols are supported.

The lightweight target Kconfig catalogue follows only unambiguous positive
conjunctions and unconditional selects. Optional references, alternatives,
negation, defaults and implications are **not** treated as mandatory dependencies.
Native Kconfig resolves full expressions, inherited menus, choices and new-symbol
defaults. Validation catches rejected requirements instead of enabling every
symbol mentioned in an expression. One catalogue is reused within an apply or
validation call, without caching it across edits to the source.

## Deliberate general baseline

| Group | Retained capabilities |
|---|---|
| Containers/firewall | Namespaces; common cgroup controllers; seccomp, BPF and IPC; bridge and bridge netfilter; veth; overlayfs; IPv4/IPv6 conntrack/NAT; common IPVS TCP/UDP ingress and round-robin scheduling; nftables; xtables compatibility and legacy iptables |
| Firewall extensions | Common state/conntrack, address type, port, owner and range matches; reject, logging, rate limiting, redirect and masquerading; FTP connection/NAT helpers |
| VPN/tunnelling | TUN/TAP, common PPP features, WireGuard, IPsec ESP/XFRM, IPIP/GRE and IPv6 tunnels |
| Filesystems/events | ext4 ACL/security, Btrfs, XFS, FUSE/CUSE, overlayfs, loop, autofs, quotas, inotify/fanotify and tmpfs xattrs |
| IPC/security | Unix sockets, SysV/POSIX IPC, futex, epoll, event/signalfd/timerfd, AIO/io_uring, normal executable formats, AppArmor/Yama/audit and common crypto APIs/algorithms |
| Routing | Policy/multicast routing, VLAN bridges, dummy/macvlan/ipvlan/vxlan, common traffic control, ipsets and IPVS infrastructure |
| Firewall sets | Common IP, network, IP/port and list set types, plus the xtables set match/target |
| Application images and terminals | SquashFS with common decompressors and xattrs, TTY and Unix98 pseudo-terminals |
| Peripherals | Existing deliberate USB hosts, storage, HID, input, audio, cameras, printers, serial adapters, Bluetooth, Wi-Fi infrastructure, removable/optical filesystems, character sets and display connector support |

This is an explicit list, not an entire-subsystem wildcard. In particular it does
not retain every NIC, Wi-Fi adapter, GPU, sound card, RAID controller or platform
driver. Network printing uses ordinary IP/socket/multicast facilities; CUPS itself
is userspace. KVM and network filesystem profiles add their relevant facilities
when detected. On a known Intel/AMD x86 host, virtualisation requires that vendor's
KVM backend even if an earlier optimised input lost it. For an unknown CPU vendor,
the working configuration supplies backend choices.

The container/firewall core is mandatory even without a runtime installed. The
remaining baseline entries are version-scoped: entries defined by the target
Kconfig are mandatory after resolution; absent historical/platform-specific
entries are not invented. Software profiles require their explicitly listed
symbols, except target-version legacy gates and firewall-extension variations. In 7.2.6,
NFSD itself includes NFSv3 server support; the old NFSD_V3 gate is requested only
when defined.
Unsupported dependencies stop the build; this policy may conservatively refuse
an older or non-x86 target instead of silently producing an incomplete kernel.

## Software evidence and extension points

Supported profiles: Docker Engine, containerd, Podman, CRI-O, LXC/LXD/Incus,
libvirt, QEMU/KVM, VirtualBox, WireGuard, OpenVPN, Tailscale, common firewall tools
(UFW/firewalld/nftables/iptables), Samba/CIFS, NFS and FUSE software.

Detection reads executable presence, installed package names and enabled systemd
units. It never starts a daemon or requires it to be active. Thus stopped Docker,
or enabled Docker in a failed state, still receives its profile. A Docker client
alone is not evidence of a local daemon. Package queries support dpkg (installed
status and multiarch names), RPM, pacman and apk. Tool failures are scan notes.
NFS/CIFS/FUSE entries in `/etc/fstab` also supply evidence when unmounted.
Enablement checks use `systemctl --root=/ is-enabled`, without a system bus.

To extend support, add a named capability set and a profile containing executable,
package and service names plus capability group names. Add evidence tests and a
native target configuration test. Avoid application conditionals in the pruning
code. The scan window shows concise general and installed-software summaries;
detailed failures identify the category, profile and up to eight rejected symbols.

## Limits

This aims to preserve normal installed-software compatibility; it cannot guarantee
every third-party, out-of-tree or undocumented kernel dependency. VirtualBox's
external vbox modules still need their own compatible build/signing process; the
profile preserves module loading, PCI, networking and timers, not a promise that
those modules work. It does not require KVM for VirtualBox.
Custom software outside the evidence mappings, hardware attached after scanning,
unbound non-network devices, alternate storage backends and unusual firewall rules
can require additional capabilities. Version-scoped baseline entries need review
when upstream renames/removes them. Saved reports are snapshots; rebuild the scan
when installed software or hardware changes.

The static module-output mapping does not implement all Kbuild syntax. Ambiguous
or unresolved bound devices cause refusal rather than guessed compatibility.
Native config-only tests verify configuration consistency, not runtime application
success, module signing, initramfs contents or firmware availability. Keep a known
working fallback kernel. No benchmark or exact installed-byte saving is inferred
from option counts: one modular option can create multiple modules, and unloaded
modules primarily cost disk/build time rather than resident memory.

See [configuration-only results](hardware-compatibility-validation.md), the
[original Docker diagnosis](container-kernel-diagnosis.md), and the preserved
[Sandy Bridge diagnosis](sandy-bridge-config-validation.md).
