# Linux 7.2.6 compatibility validation — 26 September 2026

## Scope and inputs

Configuration only: **no kernel image/module build or installation**, no GRUB
changes, no package removals, and no changes to Docker, network or firewall
configuration. Only the host Kconfig utility and its host prerequisites were
compiled with `make O=/tmp/kernel-config-tools defconfig`.

The original `/home/adrian/kernel-build` source directory was empty. The official
[Linux 7.2.6 archive](https://cdn.kernel.org/pub/linux/kernel/v7.x/linux-7.2.6.tar.xz)
was downloaded into `/tmp`, extracted and checked for version 7.2.6. Archive
SHA256: `039aef84f2b0994aeda3f4fcfc3d02ec9d7a9bbb9020ea264c43f446c860f606`.
This records the downloaded input; it does not claim a separate signature check.

- Acer Aspire C22-1600: current read-only N4505/Jasper Lake hardware and software
  scan in `tests/fixtures/acer/hardware.json`; input `/boot/config-7.0.0-34-generic`
  (SHA256 `354a25b259e8245db07f99c132a3f8325ac887d00a6fca9360105defbe2ca2b3`).
- Sandy Bridge: original i3-2310M/Broadcom tg3/Atheros ath9k hardware fixture and
  saved pre-pruning Linux 7.2.6 baseline `tests/fixtures/sandy_bridge/working.config`
  (SHA256 `94767e5cb831b6b8257ae9ddcdc20c34c1e2ddddda65df43ce81cce120907dc2`).
  This is the existing machine's saved baseline, not a newly captured Ubuntu
  generic configuration.

Both run native olddefconfig, save the target-normalized baseline, run native
streamline_config.pl against the fixture modules, run olddefconfig, restore
requirements, run olddefconfig again and validate all four requirement categories.
An additional native scenario enables every software profile together, including
NFS server/client support absent from the Acer inventory. Every subprocess has
closed stdin. GCC covers both machines; the existing Sandy
Bridge test also covers Clang/LLD and restoration of its Docker capability floor.
Temporary generated configurations are removed by the tests.

## Counts and retained functionality

| Machine | Starting y / m | Target-normalized y / m | Pruned y / m | Final y / m |
|---|---:|---:|---:|---:|
| acer | 3340 / 6708 | 3312 / 6615 | 2463 / 222 | 2501 / 371 |
| sandy_bridge | 3325 / 6614 | 3309 / 6614 | 2430 / 144 | 2463 / 292 |

Restoration adds 38 built-in and 149 modular options over raw pruning on the
Acer, and 33 built-in and 148 modular options on Sandy Bridge. The explicit
baseline has 253 seed symbols before target-version filtering and dependency
resolution. Compatibility takes priority over reducing these counts.

The September 26 review additionally protects SquashFS application images and
their common decompressors, Unix98 pseudo-terminals, and common firewall set
types with the xtables set match/target. These add five modular options to each
machine compared with the earlier result. Virtualisation validation also recovers
the known Intel/AMD x86 KVM backend when an earlier input has lost it; a dedicated
regression checks both restoration and rejection when that backend is missing.

These are **Kconfig y/m option counts**, not counts of `.ko` files, installed bytes
or measured RAM usage. Several symbols can describe one module, or one symbol
can build multiple modules. The restoration delta includes boot, hardware,
platform and installed-software requirements together, so it is a conservative
upper bound on the platform baseline's incremental option cost in these runs.
Keeping small loadable infrastructure primarily costs build time and disk space
when unused. No kernel build was performed to measure exact size or performance.

The native tests verify root/boot/device-mapper requirements, Acer r8169,
MT7921E, NVMe and i915, and Sandy Bridge tg3/ath9k (including ATH9K_PCI), AHCI and
i915. They verify the general baseline and every detected Acer software profile.
Unrelated Chelsio/QED network, AMDGPU, HDSP audio, MegaRAID and NVMe-FC drivers
remain disabled. The old broad cgroup pattern would retain BLK_CGROUP_FC_APPID,
which depends on NVME_FC; it is now explicitly excluded from container policy.

## Docker root cause, target audit and regression

The reported Docker 29.8.1 daemon reached default bridge network initialisation
but could not execute `iptables -t nat ... -m addrtype --dst-type LOCAL` because
`CONFIG_NETFILTER_XT_MATCH_ADDRTYPE` was absent. The focused historical configs
also lack NFT_COMPAT, VETH and OVERLAY_FS. The strategy was incomplete: loaded
hardware modules and a successful boot do not identify installed applications'
kernel requirements. The fix is an unconditional common platform floor plus
software profiles and independent final validation, rather than ADDRTYPE alone.

Audit against the downloaded **7.2.6 Kconfig/Kbuild**:

| Capability | Target dependency details |
|---|---|
| ADDRTYPE | Tristate under the NETFILTER_XTABLES enclosing gate; supplies the address-type extension |
| NFT_COMPAT | Tristate under NF_TABLES, depends on NETFILTER_XTABLES; different from NETFILTER_XTABLES_COMPAT (32-bit userspace translation) |
| NAT/conntrack | NF_NAT depends on NF_CONNTRACK; conntrack selects IPv4 defragmentation and conditionally IPv6 defragmentation; native Kconfig handles the conditional selection |
| nft NAT | NFT_NAT depends on conntrack and either IPv4/IPv6 table family; selects NF_NAT. NFT_MASQ also selects boolean NF_NAT_MASQUERADE |
| Legacy NAT | IP_NF_NAT/IP6_NF_NAT depend on their target legacy gates and select NF_NAT/NETFILTER_XT_NAT. The old per-family MASQUERADE symbols are compatibility aliases for the retained NETFILTER_XT_TARGET_MASQUERADE |
| Bridge filtering | BRIDGE_NETFILTER depends on BRIDGE, NETFILTER, INET and NETFILTER_ADVANCED; selects NETFILTER_FAMILY_BRIDGE and SKB_EXTENSIONS |
| Virtual Ethernet | VETH selects PAGE_POOL; the enclosing network-device infrastructure is retained |
| Container storage | OVERLAY_FS selects FS_STACK and EXPORTFS; common filesystem ACL/xattr/security support is preserved |
| IPVS | NETFILTER_XT_MATCH_IPVS depends on IP_VS, NETFILTER_ADVANCED and NF_CONNTRACK; common TCP/UDP, IPv6 and round-robin support is deliberate policy, not every scheduler |
| Isolation/resources | Required namespace/cgroup/IPC/seccomp/BPF gates and working-config security/controller choices are checked using target symbol types |

[Moby's capability checker](https://github.com/moby/moby/blob/master/contrib/check-config.sh)
provided a cross-check, including IPVS matching; historical aliases were resolved
against 7.2.6 rather than copied blindly. Docker's
[firewall documentation](https://docs.docker.com/engine/network/packet-filtering-firewalls/)
explains the bridge network firewall backends. Both nft and legacy paths remain
supported rather than assuming the currently selected userspace backend.

The exact Acer validator requirement manifest, including the working-config
container choices and unconditional dependency closure, is
[docker-linux-7.2.6.requirements.config](docker-linux-7.2.6.requirements.config).
Conditional selections and implementation defaults remain native Kconfig's
responsibility; the manifest is not an entire kernel configuration.

The focused historical regression rejects the supplied broken config. The real
source regression first produces a corrected Acer configuration, removes only
ADDRTYPE, resolves it again through olddefconfig, then requires this refusal:

```
Installed software / Docker compatibility failed: required CONFIG_NETFILTER_XT_MATCH_ADDRTYPE did not survive final configuration
```

Restoring the corrected configuration and running olddefconfig passes all
validators, including Docker networking and overlay storage. No claim of a new
runtime Docker or boot test is made, as building/installing was prohibited.

## Automated verification

Complete suite on 26 September 2026, using the environment below and an accessible
Tk display:

```text
Ran 189 tests in 175.253s
OK
```

All 189 tests passed with no skips, including four real GUI checks, native
Kconfig fixture checks, both hardware fixtures, the Clang/LLD case, every software
profile, and the Acer Docker rejection/restoration regression. The source archive
was downloaded afresh and matched the SHA256 above. The fresh Docker profile
output exactly matches the documented requirement manifest.

After explicitly closing stdin and adding timeouts to the two older native
container subprocess tests, all 16 container tests were rerun and passed in
14.553 seconds. `bash -n build-custom-kernel.sh` and `git diff --check` also passed.
No kernel was built or installed.

Session evidence: `/tmp/kernel-compatibility-tests.log`,
`/tmp/kernel-container-tests.log`, and `/tmp/kernel-compatibility-results.json`.
These temporary logs may disappear after cleanup; the results and input hashes
are recorded here for reproducibility.

Reproduce after preparing the host conf tool as described above:

```sh
KERNEL_SOURCE_726=/tmp/linux-7.2.6 \
KERNEL_KCONFIG_CONF=/tmp/kernel-config-tools/scripts/kconfig/conf \
KERNEL_BASELINE_CONFIG=/boot/config-7.0.0-34-generic \
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
```

Set `KERNEL_GENERIC_CONFIG` if the generic input has another path. A working Tk
X display is needed for the four real GUI checks. The existing historical
installed-config test accepts `KERNEL_BASELINE_CONFIG` because this machine no
longer has `/boot/config-7.2.0-custom`; its original focused omission fixture is
still tested. Shell integration tests use fake tools and temporary directories;
they do not perform their simulated installations on the host.

Coverage includes both machines, stopped/failed-enabled Docker, all software
profiles, dpkg/RPM/pacman/apk evidence, configured unmounted network/FUSE
filesystems, no-runtime baseline retention, optional-reference exclusion,
noninteractive NEW defaults, mixed-case symbols, bool/tristate handling, genuine
capability rejection, hardware dependency closure, unrelated-driver pruning and
pre-compilation shell refusal. Full diagnostics remain in the test log.

See [design and limitations](software-compatibility.md). Package discovery and
profile lists cannot cover every private installation or undocumented dependency;
VirtualBox/out-of-tree modules, unusual storage/firewall modes, firmware and
runtime behaviour still need separate validation. The existing Sandy Bridge
diagnostic documentation remains intact.

## Files changed

- `hardware_optimizer.py`: compatibility groups, software evidence/profiles,
  all-bus hardware requirements, category validation and scan reporting.
- `build-custom-kernel.sh`: closed-stdin native config processing and validation
  messages covering boot, hardware, platform and software.
- `kernel-manager-gui.py`: concise Hardware Optimised explanation; the existing
  scan window displays the expanded report.
- `tests/test_platform_compatibility.py`, `tests/test_target_platform.py`,
  `tests/fixtures/acer/{hardware.json,README.md}`: new profile/baseline/hardware
  tests, real source validation and Acer inventory.
- `tests/test_container_optimizer.py`, `tests/test_hardware_optimizer.py`,
  `tests/test_sandy_bridge_optimizer.py`, `tests/test_kernel_manager.py`,
  `tests/fixtures/container/Kconfig`: preserved/extended regressions, explicit
  target-source/conf overrides, new baseline contract and IPVS fixture.
- `README.md`, `docs/software-compatibility.md`, this report,
  `docs/docker-linux-7.2.6.requirements.config` and
  `docs/container-kernel-diagnosis.md`: policy, limitations, audit, results and
  Docker requirement manifest. `docs/sandy-bridge-config-validation.md` is kept.
