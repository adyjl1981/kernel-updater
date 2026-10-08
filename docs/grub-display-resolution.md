# GRUB Display Resolution

Tools → GRUB Display Resolution reads active assignments in `/etc/default/grub`
without authentication or executing that file. It shows configured values, not
proof of the mode actually used at boot. Competing display assignments in
`/etc/default/grub.d/*.cfg`, duplicate active keys, dynamic assignments and
unsupported configuration structures require manual resolution first.

Safe defaults are `auto` with **Keep GRUB graphics mode for Linux** unchecked.
Select one of three common resolutions: `1024x768`, `1280x720`, or `1920x1080`.
The selector also offers `auto` as the safe default. Each explicit resolution is
saved with `,auto`. Existing settings outside these choices remain visible in
the current-settings label; the selector defaults to `auto` and replaces them
only when you explicitly apply. Unchecking keep comments out an active
`GRUB_GFXPAYLOAD_LINUX` assignment so the distribution chooses its default.

Linux `/sys/class/graphics/fb0/virtual_size` reports a Linux framebuffer, which
may be provided by a kernel graphics driver. It does not prove firmware/GRUB
mode support. Use `videoinfo` at the GRUB command prompt to inspect available
modes. Syntax validation cannot guarantee hardware support. `keep` can cause
early boot display problems on some systems.

Only clicking **Apply Display Settings and Regenerate GRUB…** and confirming the
preview starts GUI sudo authentication. Merely selecting options or refreshing
never writes or regenerates. The helper revalidates the request, checks the
original snapshot, takes the existing shared GRUB transaction lock, backs up
both defaults and generated configuration, and replaces defaults atomically.
Comments and unrelated settings (including saved kernel selection) are retained.

Supported installed layouts:

- `/boot/grub/grub.cfg`: `update-grub`, or `grub-mkconfig -o /boot/grub/grub.cfg`.
- `/boot/grub2/grub.cfg`: `grub2-mkconfig -o /boot/grub2/grub.cfg`.

Missing tools, ambiguous layouts and symlink targets are refused before changes.
EFI forwarding stubs are never regeneration targets. Nonstandard layouts need
manual handling according to the distribution's instructions.

On regeneration failure the transaction attempts to restore both files. The
operation log reports the error, rollback result and unique backup paths. If
rollback is incomplete, restore those backup files to the original paths with
administrator privileges before rebooting; do not overwrite concurrent edits
without reviewing them. If a supported custom mode still causes display issues,
select `auto` and uncheck keep, then apply again. If the machine cannot boot,
use its recovery menu or live media to access the installed system and restore
the saved defaults and generated menu. Successful application does not reboot.

References: [GNU GRUB gfxmode](https://www.gnu.org/software/grub/manual/grub/html_node/gfxmode.html),
[GNU GRUB gfxpayload](https://www.gnu.org/software/grub/manual/grub/html_node/gfxpayload.html),
[Fedora GRUB configuration layout](https://fedoraproject.org/wiki/GRUB_2).
