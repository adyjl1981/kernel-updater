This fixture records the failing Intel Core i3-2310M laptop from September 20,
2026. `hardware.json` and `working.config` are the saved hardware inventory and
pre-localmodconfig Linux 7.2.6 baseline from that machine. The small drivers/net
files model the relevant target Kconfig/Kbuild semantics and unrelated options
which the old all-network-symbols traversal incorrectly required.

The full-source test uses these saved inputs with Linux 7.2.6's existing conf
executable and streamline_config.pl in a temporary output directory. It only
processes configuration; it never invokes a kernel build, installation, or GRUB.
