# Sandy Bridge configuration validation regression

## Evidence and root causes

The supplied `/home/adrian/log.txt` records an i3-2310M machine with Broadcom
BCM57785 (14e4:16b5), Atheros AR9287 (168c:002e), SATA AHCI, Intel graphics,
EHCI/xHCI, and ext4 on `/dev/mapper/ubuntu--vg-ubuntu--lv`. Its saved inventory
and pre-pruning target config are captured in `tests/fixtures/sandy_bridge`.
READY means the inventory is adequate to attempt optimisation; it is not a
claim that the subsequent kernel configuration has passed validation.

1. Linux 7.2.6's `scripts/kconfig/Makefile` implements `localmodconfig` as
   `streamline_config.pl` followed by **`conf --oldconfig`**. Pruning can expose
   previously hidden NEW symbols even after an initial `olddefconfig`. The log
   shows `GPIO_BT8XX` and `SND_SE6X` asking for input, then reporting EOF.
   A later `olddefconfig` cannot undo that interactive invocation.
2. `networking_baseline()` seeded every enabled definition under `net/` and
   `drivers/net/`, not just detected adapters and deliberately retained
   compatibility features. The saved broad distro baseline produced **2,465**
   network requirements. It then recursively treated every word in `depends`,
   `select`, `imply`, `default`, and `def_bool` expressions as mandatory.
   Alternatives, negations, optional conditions, and derived helper defaults
   therefore pulled in unrelated features. Config blocks also included following
   menu properties, and scanning every architecture merged unrelated definitions.
   Re-running the old code against the saved failed config reproduced the exact
   PCMCIA/CAN/Chelsio/DCA/QED/NTB/SunRPC/SoundWire rejection list. Both actual
   network adapters already passed its separate driver check.

The earlier mixed-case correction is necessary (e.g. `MT792x_LIB`) and remains.
It exposed more legitimate symbol names to the overbroad traversal but did not
cause the incorrect dependency model. Initial warnings such as
`NETFILTER_NETLINK=m` come from using an older distro config against a target
where types changed. Native `olddefconfig` normalizes that input. New required
values now also use the target bool/tristate definitions: a bool must be `y`,
while a loadable driver/helper may be `m`.

## Fix

The build script now invokes the target's pruning script directly, supplying
`LSMOD`, `SRCARCH`, `srctree`, and `objtree`, then runs **`olddefconfig` with stdin
closed**. It keeps the existing compiler arguments and runs compatibility
restoration and both validation checkpoints as before. No `yes` pipeline,
interactive target, or forced all-options configuration is used. Pruning errors
still stop the build before compilation.

Network roots are the declared compatibility set and target Kbuild mappings for
detected adapters. Required edges include simple positive dependencies,
enclosing positive `if` gates, and unconditional selects. Architecture-specific
files are restricted to the baseline's architecture; shared `arch/Kconfig`
remains included. The full expression semantics (alternatives, negation,
conditional selects, implications, defaults) belong to native Kconfig, not a
regex dependency evaluator. The requested capabilities are checked **after**
that resolution. Optional names merely encountered in expressions are not
mandatory. Required hidden helpers may resolve to modules when an unrelated
built-in consumer has been removed; explicit built-in requirements remain
built-in. Unknown driver mappings and missing required capabilities fail closed.

The PCI ath9k bus gate is checked separately: `ATH9K=m` alone does not ensure
that `ath9k` includes its PCI transport. Kbuild's conditional `pci.o` membership
is not mistaken for a standalone module. The main module mapping search is
restricted to network drivers, avoiding unrelated same-named objects.

The same separation applies to container requirements without changing the
explicit `CONTAINER_REQUIRED` floor. Baseline extensions preserve user-selectable
container features, not orphaned hidden implementation helpers. `_NS` no longer
mistakes RPMSG **name service** for a Linux namespace. The target integration
also tests adding the full Docker capability floor to this ext4 fixture.

## Linux 7.2.6 config-only result

Using the existing target `scripts/kconfig/conf` and `streamline_config.pl`,
Clang/LLD, the saved hardware fixture, and a temporary output directory:

| Hardware/capability | Result |
| --- | --- |
| BCM57785, PCI 14e4:16b5 | `CONFIG_TIGON3=m` → `tg3.ko` |
| AR9287, PCI 168c:002e | `CONFIG_ATH9K=m` → `ath9k.ko`; `CONFIG_ATH9K_PCI=y` |
| Wi-Fi helpers | `ATH9K_HW=m`, `ATH9K_COMMON=m`, `ATH_COMMON=m`, `MAC80211=m`, `CFG80211=m` |
| Broadcom PHY infrastructure | `PHYLIB=y` |
| ext4 and device-mapper | `EXT4_FS=y`, `BLK_DEV_DM=y`, `BLK_DEV_DM_BUILTIN=y` |
| SATA AHCI | `SATA_AHCI=m` |
| Intel graphics | `DRM_I915=m` |
| USB controllers | `USB_EHCI_PCI=m`, `USB_XHCI_PCI=m` |

The target's PCI tables confirm both device IDs; the target Makefiles confirm
module mappings and the ath9k PCI composite-object gate. Boot/network validation
passes. CAN PCMCIA, Chelsio, NTB networking, QED, SunRPC, and SoundWire remain
disabled. Modular symbols drop from **6,614 to 213** before the optional
container-floor test. These are configuration results, not a kernel build or a
boot/runtime test on the laptop.

## Regression coverage

- Real recorded hardware/baseline fixture and a small Kconfig/Kbuild fixture.
- Exact target driver mappings, PHY/Wi-Fi helpers, and PCI bus gate.
- Unrelated baseline options, optional expressions, menu leakage, foreign-arch
  definitions, and legitimate helper demotion.
- Mixed-case names and target boolean/tristate semantics.
- Negative tests for missing Ethernet, Wi-Fi, bus support, helpers, LVM and ext4.
- Native `--oldconfig` EOF reproduction versus default-setting `--olddefconfig`
  with closed stdin, including NEW bool, tristate, and string symbols.
- Pruning failure stops before compilation; existing final network/container
  rejection tests remain in force.
- Real Linux 7.2.6 config-only processing, with and without the container floor.

No installed config or kernel source tree is modified by the config-only test.
No kernel is built or installed and no GRUB operation is performed.

## Complete test results

Final command: `python3 -B -m unittest discover -s tests -v`, with display access
for the real Tk tests.

**171 tests run in 132.118 seconds: 170 passed, 1 skipped, 0 failures/errors.**
The single skip is the pre-existing installed-config comparison test, because
`/boot/config-7.2.0-custom` and `/boot/config-7.2.6-optimized` are unavailable.
The new Linux 7.2.6 Sandy Bridge config-only test **ran and passed**, including
its additional container-floor validation. `bash -n build-custom-kernel.sh` and
`git diff --check` also passed.
