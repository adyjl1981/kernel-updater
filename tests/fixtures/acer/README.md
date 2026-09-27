# Acer Aspire C22-1600 fixture

Read-only scan captured September 25, 2026 from the Acer N4505/Jasper Lake machine:
Intel i915 display, Realtek r8169 Ethernet, MediaTek mt7921e Wi-Fi and Kingston
NVMe root storage. Software evidence includes Docker and containerd even though
the reported Docker bridge initialisation failed. The report contains no daemon
status dependency. PCI/USB bound module providers are saved for hardware checks.

The existing `../container/7.2.6-optimized.config` focused fixture records the
missing ADDRTYPE/NFT_COMPAT/VETH/OVERLAY_FS regression. Real integration tests use
an Ubuntu generic input (selectable with KERNEL_GENERIC_CONFIG), native Linux
7.2.6 Kconfig and this saved inventory. They also explicitly delete ADDRTYPE from
the corrected config and require a Docker-specific pre-compilation refusal.
