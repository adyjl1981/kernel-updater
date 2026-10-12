# Memory Management (Linux)

The Ubuntu-themed tab displays unprivileged snapshots using Refresh Memory.
RAM total/available and swap total/free come from /proc/meminfo; active devices,
used size and priority come from /proc/swaps. Both interfaces report KiB despite
the Linux `kB` label. Values are shown as exact bytes plus rounded MiB.
Current vm.swappiness is displayed separately from the editable new value.
No factory defaults are inferred.

Configuration discovery covers /etc/fstab, top-level systemd swap units in
/etc, /usr/lib and generated units in /run, plus zram devices in sysfs.
Resolvable UUID/LABEL aliases merge with canonical device rows, including after
disabling. Observed configuration does not establish boot enablement. Unresolved UUID,
LABEL and device aliases, drop-ins, generators, encrypted swap and other managers
may need additional discovery. Unknown values are displayed explicitly.

## One-time administrator setup

Review `memory_helper.py` and `install-memory-helper.sh`, then choose **Helper Setup**
and **Install Helper** in the dialog. Setup requests administrator authentication,
runs in the background with an operation log, verifies the installed copy and
refreshes memory readiness. Cancel closes the dialog without running setup.
Authentication or installation failures appear in the log and a result dialog.

Alternatively, run:

```sh
sudo bash /home/adrian/Kernel/install-memory-helper.sh
```

This installs only `/usr/local/libexec/kernel-manager-memory.py` as root:root,
mode 0644, and creates `/var/lib/kernel-manager-memory` as a private root directory.
It does not change swap state, swappiness, packages or sudo permissions.
Refresh Memory afterwards. Repeat setup after changing the helper source; the GUI
checks that the installed copy matches this checkout. The application itself
continues to run as your normal user. No privileged helper runs from the checkout.

Every change requires confirmation, then normal sudo authentication (sudo may
reuse an existing authentication session). Only the installed helper is invoked,
using the system Python in isolated mode with site loading disabled (`-I -S`).
A bounded strict JSON request travels on stdin. The helper imports only stdlib,
verifies its own root-owned path and directory ancestry, and accepts only three
request types: swappiness, restore_swappiness and individual swap enable/disable.
The production entry point exposes no fixture root, command, destination file,
formatting, resize or bulk swap action. No passwordless sudoers rules are added.

## Swappiness

Enter an integer from 0 to 200 and explicitly choose Temporary or Persistent.
Temporary changes only the running kernel value. Persistent additionally writes
`/etc/sysctl.d/99-kernel-manager-memory.conf`; other sysctl files remain intact.
Competing definitions (including slash syntax and globs) cause persistence to be
refused; use Temporary or resolve the conflict as administrator. Masked or
untrusted configuration may also require administrator review. Kernel rejection
is reported and the prior setting is restored when possible.

Before writes, the helper records the previous runtime value and exact dedicated
configuration contents in `/var/lib/kernel-manager-memory/swappiness.json`.
Configuration and state replacement are atomic and synced. A write failure attempts
to restore both previous values. If recovery is incomplete the record is retained
and further changes are blocked until recovery. **Restore Previous Swappiness**
restores the most recent recorded setting, including removing a configuration file
created by that change. It refuses to overwrite external edits or infer runtime
state from a previous boot. There is no claimed factory reset.

After a crash in the same boot, use Restore Previous Swappiness. Across boots,
review the root-owned recovery record as administrator: `old_text` is the exact
prior dedicated configuration (null means it did not exist), `old_value` is the
previous runtime value, and `phase` records whether application completed.
Recovery after a reboot requires an explicit administrator decision about runtime
and boot settings; the GUI will not guess. Preserve the record until that review
is complete. Restore is retryable after a partial failure.

## Swap on/off

Select one device and choose Enable Selected or Disable Selected. This changes
only current runtime state; fstab, systemd units and zram configuration stay intact.
A system manager can override runtime state. The helper verifies fresh active state,
canonical path, file/device identity, trusted ownership and directory permissions.
Files with writable ancestry, writable regular files, aliases and hard links are
refused. Conventional root:disk block-device permissions are accepted because the
disk group already has raw-device privileges; world-writable blocks are refused.

Enable supports trusted fstab swap entries (including resolvable UUID/LABEL),
initialized zram, or a device previously disabled by this helper. It never formats,
allocates, resizes or discards storage. An inactive systemd-only device may remain
read-only and must be enabled by its owning manager. A saved priority is retained
when re-enabling a helper-disabled device; configured fstab priorities are honored.

Disable re-reads /proc/meminfo and /proc/swaps in the privileged process and requires
MemAvailable >= twice the selected device's used bytes plus the larger of 512 MiB
or 25% of total RAM. Missing, malformed or insufficient counters cause refusal.
Other swap capacity is not counted as safe RAM. Nonzero resume devices, hibernation
boot hints, uncertain hibernation support, or initramfs resume configuration cause
refusal. The checks are deliberately conservative; concurrent workload changes
mean headroom is not a guarantee against memory pressure. A failed swapoff is
reported without claiming the device is disabled. Refresh the snapshot afterwards.

A root-owned disabled-device record allows re-enabling the same unchanged device
even when it was not in fstab. To undo Disable, select the device and Enable it;
to undo Enable, Disable it subject to the same current safety checks. The tab
retains discovered paths for this session. After restarting the application, an
unconfigured device can be rediscovered with **Add Device to List…**, using its
previous path. Adding a path only reads metadata and grants no helper permissions.

## Check Swap Settings

This button takes a fresh read-only snapshot and opens a scrollable report. It
requires no installed helper or authentication. It explains RAM total/available,
active swap total/free/used and individual types, filesystem available space for
files, current swappiness, hibernation hints and memory PSI when available.
PSI avg10 is percentage time stalled over ten seconds; malformed/non-finite values
are rejected and unavailable sources remain unknown. Zram logical capacity is
distinguished from disk capacity. Filesystem available space is not quota or
allocation eligibility proof. Swap inventories and memory counters are sampled
separately and can change during collection.

Recommendations are conditional on observations and representative workloads.
Below 10% available RAM, over 80% swap usage, and PSI above 1% are explainable
triage thresholds, not sizing targets. Existing swap usage alone does not prove
current pressure. Missing or inconsistent RAM/swap counters withhold sizing advice.
There is no universal RAM multiplier, guaranteed optimum, or automatic tuning.
The report explains increasing versus decreasing swappiness: increasing favours
swapping application memory relative to dropping file cache; decreasing favours
keeping application memory and reclaiming more cache. Neither direction guarantees
better performance. It explains that 0 delays swapping without disabling it, 100
represents roughly equal I/O cost, and changing the value does not resize swap or
immediately move existing pages. Advice differs for disk swap, zram, mixed types,
and missing capacity/inventory. Small trials (10 points either way, bounded by
0–200) are comparisons, not optimum targets. The report gives the app's Temporary,
Apply, confirmation and Restore steps and asks for workload/PSI/responsiveness
comparisons before persistence. Hibernation requires
verification of the designated target/image, encryption and resume configuration.

## Mouse wheel

The shared wheel router handles Linux Button-4/5 and MouseWheel before native
scrollable widget bindings. It uses pointer location rather than keyboard focus,
scrolls the nearest overflowing Text/Treeview/Listbox/Canvas, and bubbles to an
ancestor at the edge. Labels/buttons inside a canvas scroll that canvas. Combo
boxes, spin boxes and sliders retain native value-control behavior. Widgets in
later-created dialogs acquire the same routing when mapped; consumed scrolls do
not run a second native scroll binding.

## Validation and references

Tests use temporary proc/sysfs/fstab/sysctl/state fixtures, fake swap executables,
and mocked GUI/authentication. They cover strict requests, stale snapshots,
ownership/symlink rejection, memory and hibernation refusals, configuration conflicts,
atomic write failures, rollback, restore and confirmation cancellation. No real swap
changes or privileged writes are used. A real GUI layout test runs only when a
display is available; installer validation uses shell syntax checking only.

The swappiness range follows the [Linux kernel documentation](https://docs.kernel.org/admin-guide/sysctl/vm.html#swappiness).
Isolation flags follow the [Python command-line documentation](https://docs.python.org/3/using/cmdline.html#cmdoption-I).
Swapfile restrictions follow [swapon(8)](https://man7.org/linux/man-pages/man8/swapon.8.html).
Persistence checks account for [sysctl.d configuration](https://www.freedesktop.org/software/systemd/man/latest/sysctl.d.html)
and procps compatibility configuration.

Recommendations name the observed outcome. A conditional increase range requires
low available RAM, nearly exhausted/absent swap and observed PSI stalls together.
It adds a buffer of max(512 MiB, half observed swap usage), with one further buffer
as the upper bound; this is an explicit trial assumption. One instant cannot justify
reducing swap or declaring it unnecessary. Recommendations remain read-only.
The application provides no swap-size adjustment controls.

Before installing the helper, manually validate authentication, theme/layout and
wheel routing on a display. Validate swap on/off and swappiness in a disposable
virtual machine before relying on privileged operations.
