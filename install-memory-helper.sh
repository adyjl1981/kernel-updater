#!/usr/bin/env bash
# Explicit administrator setup only. Does not alter any swap or sysctl settings.
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo 'Run this reviewed installer as administrator.' >&2; exit 1; }
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source_file="$script_dir/memory_helper.py"
[[ -f "$source_file" && ! -L "$source_file" ]] || { echo 'Missing helper source.' >&2; exit 1; }
check_directory() {
    local path="$1" owner mode
    [[ -d "$path" && ! -L "$path" ]] || { echo "Unsafe directory: $path" >&2; exit 1; }
    owner="$(/usr/bin/stat -c %u -- "$path")"
    mode="$(/usr/bin/stat -c %a -- "$path")"
    [[ "$owner" == 0 ]] && (( (8#$mode & 8#022) == 0 )) || { echo "Untrusted directory: $path" >&2; exit 1; }
}
for path in / /usr /usr/local /var /var/lib /etc /etc/sysctl.d; do check_directory "$path"; done
for path in /usr/local/libexec /var/lib/kernel-manager-memory; do
    if [[ ! -e "$path" && ! -L "$path" ]]; then
        /usr/bin/install -d -o root -g root -m 0755 -- "$path"
    fi
    check_directory "$path"
done
/usr/bin/chmod 0700 /var/lib/kernel-manager-memory
target=/usr/local/libexec/kernel-manager-memory.py
if [[ -e "$target" || -L "$target" ]]; then
    [[ -f "$target" && ! -L "$target" && "$(/usr/bin/stat -c %u -- "$target")" == 0 ]] || { echo 'Unsafe existing helper.' >&2; exit 1; }
fi
stage="$(/usr/bin/mktemp /usr/local/libexec/.kernel-manager-memory.XXXXXX)"
trap '/usr/bin/rm -f -- "$stage"' EXIT
/usr/bin/install -o root -g root -m 0644 -- "$source_file" "$stage"
/usr/bin/mv -fT -- "$stage" "$target"
echo 'Installed the root-owned memory helper. Refresh Memory in Kernel Manager.'
echo 'No swap state or swappiness setting was changed.'
