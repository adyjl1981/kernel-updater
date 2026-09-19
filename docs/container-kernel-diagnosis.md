# Container kernel diagnosis — 19 September 2026

Read-only inputs: `/boot/config-7.2.0-custom`, `/boot/config-7.2.6-optimized`,
`/lib/modules/7.2.6-optimized/{kernel,modules.alias,modules.dep,modules.builtin,modules.builtin.modinfo}`,
and the existing `/home/adrian/kernel-build/linux-7.2.6` Kconfig/Kbuild source.

## Finding

There are **no enabled-to-disabled losses** in the requested container surface,
or in the full target `net/` and `drivers/net/` baseline dependency closure.
The two supplied configurations both lack the features below. Thus the supplied
7.2.0-custom configuration cannot establish that the same Docker bridge setup
worked on that kernel. Successful boot and working physical networking do not
establish container compatibility.

`CONFIG_NETFILTER_XT_MATCH_ADDRTYPE=n` explains the reported addrtype revision
warning: `xt_addrtype` registers that xtables match and its `ipt_addrtype` and
`ip6t_addrtype` aliases. It is absent. `CONFIG_NFT_COMPAT=n` also removes the
xtables-over-nftables extension path used with iptables-nft. The native nft FIB
expression modules are absent too. Restoring ADDRTYPE alone is insufficient.
The supplied error and static audit establish missing capabilities; no live
Docker start, firewall mutation, or module loading was attempted.

## Exact missing common container capabilities

Every entry below is disabled in **both** configs. Every corresponding `.ko`
is absent in 7.2.6-optimized, with no entry in `modules.dep` or built-in metadata.
All option names have the `CONFIG_` prefix.

| Option | Missing module | Purpose |
|---|---|---|
| NETFILTER_XT_MATCH_ADDRTYPE | xt_addrtype | Address type matching; observed warning |
| NFT_COMPAT | nft_compat | Xtables extensions through nftables |
| NETFILTER_XT_MATCH_CONNTRACK | xt_conntrack | Connection-state match |
| NETFILTER_XT_MATCH_COMMENT | xt_comment | Annotated runtime/CNI rules |
| NETFILTER_XT_MARK | xt_mark | Packet mark match/target |
| NETFILTER_XT_NAT | xt_nat | Xtables DNAT/SNAT |
| NETFILTER_XT_TARGET_MASQUERADE | xt_MASQUERADE | Xtables masquerading |
| NETFILTER_XT_TARGET_CHECKSUM | xt_CHECKSUM | Runtime/CNI checksum rules |
| IP_NF_NAT | iptable_nat | Legacy IPv4 NAT table |
| IP6_NF_NAT | ip6table_nat | Legacy IPv6 NAT table |
| IP_NF_MANGLE | iptable_mangle | Legacy IPv4 mangle table |
| IP6_NF_MANGLE | ip6table_mangle | Legacy IPv6 mangle table |
| IP_NF_RAW | iptable_raw | Legacy IPv4 raw table |
| IP6_NF_RAW | ip6table_raw | Legacy IPv6 raw table |
| NFT_FIB | nft_fib | Shared nft FIB expression support |
| NFT_FIB_IPV4 | nft_fib_ipv4 | Native nft IPv4 address/FIB lookup |
| NFT_FIB_IPV6 | nft_fib_ipv6 | Native nft IPv6 address/FIB lookup |
| VETH | veth | Container virtual Ethernet pairs |
| OVERLAY_FS | overlay | Overlay filesystem/container storage |

This is a conservative common capability set supporting nft and legacy userspace,
not a claim that every rule backend uses every module simultaneously. Overlayfs
is needed for overlay storage; other storage drivers have different requirements.

Additional optional network modes also lack `CONFIG_IP_SET`, `CONFIG_IP_VS`,
`CONFIG_VXLAN`, `CONFIG_MACVLAN`, `CONFIG_IPVLAN`, `CONFIG_DUMMY`, and
`CONFIG_BRIDGE_VLAN_FILTERING` in both configs. These are not losses either.
The optimiser preserves these modes when enabled in the baseline; the common
bridge/overlayfs floor does not automatically enable every optional network mode.

## Present in both configs

- `NET`, `INET`, `IPV6`, `NETDEVICES`, `NET_CORE`, `NETFILTER`,
  `NETFILTER_ADVANCED`, `NETFILTER_NETLINK`: enabled.
- `NF_TABLES=m`, `NF_TABLES_INET=y`, `NF_TABLES_IPV4=y`, `NF_TABLES_IPV6=y`.
  The IPv4/IPv6 family options are booleans, not separate modules.
- `NF_CONNTRACK=m`, `NF_NAT=m`, `NF_NAT_MASQUERADE=y`,
  `NF_DEFRAG_IPV4=m`, `NF_DEFRAG_IPV6=m`, `NFT_CT=m`, `NFT_NAT=m`, `NFT_MASQ=m`.
- `NETFILTER_XTABLES=m`, `NETFILTER_XTABLES_COMPAT=y`,
  `NETFILTER_XTABLES_LEGACY=y`, IPv4/IPv6 iptables legacy gates and filter tables.
  `NETFILTER_XTABLES_COMPAT` is distinct from the missing `NFT_COMPAT`.
- `BRIDGE=m`, `BRIDGE_NETFILTER=m`; `UNIX=y`, `PACKET=y`.
- `NAMESPACES`, `UTS_NS`, `IPC_NS`, `USER_NS`, `PID_NS`, `NET_NS`, `TIME_NS`.
- `CGROUPS`, memory/pids/device/freezer/scheduler/CPU accounting controllers,
  `CPUSETS`, `FAIR_GROUP_SCHED`, `CFS_BANDWIDTH`, `BLK_CGROUP`, `CGROUP_BPF`,
  `BPF`, `BPF_SYSCALL`.
- `SECCOMP`, `SECCOMP_FILTER`, architecture seccomp support, `KEYS`,
  `SYSVIPC`, `POSIX_MQUEUE`, `TMPFS`, `TMPFS_POSIX_ACL`.

Actual present modules include `bridge`, `br_netfilter`, `nf_conntrack`,
`nf_nat`, `nf_tables`, `nft_ct`, `nft_nat`, `nft_masq`, `nft_chain_nat`,
`x_tables`, `xt_tcpudp`, `ip_tables`, `ip6_tables`, `iptable_filter`,
`ip6table_filter`, and both defragmentation modules. `modules.dep` confirms
NAT -> conntrack -> IPv4/IPv6 defragmentation and bridge-netfilter -> bridge ->
STP/LLC dependencies. `modules.alias` has `nfnetlink-subsys-10 -> nf_tables`,
but no addrtype, nft match/target/FIB, veth, or overlay aliases.

## Runtime detection and fix

Installed package evidence: Docker CE `5:29.8.0-1~ubuntu.24.04~noble` and
containerd.io `2.3.5-1~ubuntu.24.04~noble`. `dockerd` and `containerd` exist.
`systemctl --root=/ is-enabled` reports `enabled` for `docker.service`,
`docker.socket`, and `containerd.service`, agreeing with `/etc/systemd/system`
enablement symlinks. Offline inspection avoids the unavailable system bus.
Hardware Optimised mode should, and now does, detect both runtimes.

The scanner records executable, installed-package, and enabled-unit evidence for
Docker, containerd, Podman, CRI-O, and LXC/LXD/Incus. It does not need a running
daemon or loaded modules. A Docker client alone is not treated as a local daemon.
Evidence survives the saved JSON hardware report used throughout the build.

For a detected runtime, the optimiser retains enabled baseline namespaces,
cgroups/controllers, IPC, seccomp, overlayfs, BPF, security support, and their
referenced baseline dependencies, together with the existing broad network
baseline. It additionally requests the common capability floor above, repairing
an incomplete baseline rather than silently inheriting it. Target Kconfig types
determine `y` for booleans and `m` for new tristates; baseline built-ins remain
built-in. Newly introduced legacy gates are requested when defined.

Real `olddefconfig` remains the dependency evaluator. Both existing validation
stages now check container requirements: after restoration and after all final
configuration changes, before compilation. Rejected dependencies, missing target
symbols, disabled requirements, and built-in demotions fail with named options.
A detected runtime without a baseline or target source fails closed.

## Verification

Regression fixtures capture the real configs' relevant settings. Tests cover
inherited omissions, individual removal of every required capability, broader
baseline dependencies, runtime detection, JSON persistence, missing source and
unknown symbols, target boolean conversion, actual Kconfig resolution/dependency
rejection, and final-validation refusal before the mocked compilation stage.
The installed 7.2.6 Kconfig/conf is also exercised on a temporary copy of the
complete optimized config; no kernel compilation is involved.

The read-only `iptables-translate` diagnostic could not initialize nft under the
restricted environment and crashed; it supplied no additional evidence. No live
firewall reproduction or post-install Docker test is claimed.

References: [Docker firewall behavior](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
and [Moby's kernel capability checker](https://github.com/moby/moby/blob/master/contrib/check-config.sh).
Target 7.2.6 Kconfig/Kbuild definitions take precedence over older symbol names
in upstream checklists.

Final complete suite: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`
— **108 tests passed in 49.610 seconds; zero failures, errors, or skips**.
Shell syntax checks passed for the build/install/desktop scripts; `git diff --check`
passed. Full run output is `/tmp/kernel-tests-final.log`.

No Docker configuration, `/boot`, GRUB, installed kernel/module tree, or initramfs
was modified. No kernel was built or installed; no commit or push was performed.
