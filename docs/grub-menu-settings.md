# GRUB Boot Menu settings

The Tools tab reads `/etc/default/grub` at startup and with **Refresh GRUB
Setting**. Its three radio buttons only select a proposed setting. **Apply GRUB
Setting** shows a diff and asks for confirmation before GUI sudo authentication.

| Choice | GRUB_TIMEOUT_STYLE | GRUB_TIMEOUT |
| --- | --- | --- |
| Hidden | hidden | 0 |
| Show for 5 seconds | menu | 5 |
| Show for 15 seconds | menu | 15 |

These follow the [GNU GRUB timeout documentation](https://www.gnu.org/software/grub/manual/grub/html_node/Simple-configuration.html).
Active `GRUB_HIDDEN_TIMEOUT` and `GRUB_HIDDEN_TIMEOUT_QUIET` legacy assignments
are commented out, retaining their original text. Existing comments, quoting,
indentation and unrelated assignments are preserved where practical.
`GRUB_DEFAULT=saved`, `GRUB_SAVEDEFAULT`, and the `grubenv` saved entry are untouched.

These settings describe ordinary boot behaviour. Ubuntu's `00_header` can use
`GRUB_RECORDFAIL_TIMEOUT` for failed-boot recovery and EFI filesystems where the
recordfail environment cannot be written. Its `30_os-prober` can force a visible
menu when another OS is detected. Firmware-button settings can also override the
normal timeout. Kernel Manager preserves these exceptions, rather than disabling
recovery or changing which operating systems are discovered. This behaviour was
checked against the installed Ubuntu `/etc/grub.d/00_header` and
`/etc/grub.d/30_os-prober` scripts. See also
[Ubuntu's GRUB configuration documentation](https://help.ubuntu.com/community/Grub2/Setup).

Custom timings have no selected radio button until the user chooses an option.
Malformed, duplicate, dynamic, conditional, or conflicting drop-in menu settings
are refused rather than evaluated as shell code or silently guessed.

The privileged helper checks that the confirmed configuration has not changed,
backs up both `/etc/default/grub` and `/boot/grub/grub.cfg` beside their originals
with unique names, replaces the defaults atomically, and runs `update-grub`.
The transaction retains ownership, permissions and file metadata. On failure it
restores both snapshots without depending on a second successful `update-grub`.
If an external edit or a filesystem error prevents safe restoration, the log
reports the problem and retained backup paths. A lock serializes menu-setting
transactions; unrelated external GRUB tools do not share that lock.

Regression tests use temporary files and mocked authentication. They never write
system GRUB files or run the real `update-grub`.
