# Automatic build jobs

The Build tab enables **Automatic** by default, including for older preferences
that only stored a jobs number. Uncheck it to use **Manual jobs**; your manual
number and the Automatic choice are saved independently. Direct script runs
also choose automatically unless you pass `--jobs N`.

Automatic mode reads usable CPU affinity and `MemAvailable`, and applies
readable CPU quotas and remaining memory limits in the standard cgroup v2
mount at `/sys/fs/cgroup`, including ancestor limits. CPU-count detection has
a fallback for systems without affinity support. Custom cgroup mounts and
cgroup v1 limits are not detected; use a manual override on such constrained
systems. Swap is not treated as build capacity. If memory information is
unavailable, the fallback is one job.

The policy reserves the larger of 1 GiB or 10% of available memory, then budgets:

| Build options | Memory allowance per job |
| --- | --- |
| Standard GCC/Clang | 1 GiB |
| Full debug info | 2 GiB |
| Clang ThinLTO | 3 GiB |
| ThinLTO and full debug info | 4 GiB |

The count is the smaller of the usable CPU count and the memory-budget count,
with a minimum of one job. These are conservative estimates, not measured
compiler peaks or a guarantee against running out of memory. Final linking
can still need more memory than one compiler job. ThinLTO backend threads are capped to the selected job count through the
linker command, preserving the kernel's architecture-specific linker flags.

The GUI logs an estimate when Start Build is clicked. The script checks again
immediately before compilation, after downloads and configuration, and logs
its final choice and reasoning. It keeps this count fixed during that build;
it does not dynamically resize a running make process. Manual `--jobs N`
overrides are respected without resource-based clamping. If the automatic
probe fails, the script reports this and falls back to one job.

The resource interfaces and linker thread option are documented in the
[Linux cgroup v2 manual](https://docs.kernel.org/admin-guide/cgroup-v2.html) and
[Clang ThinLTO manual](https://clang.llvm.org/docs/ThinLTO.html#controlling-backend-parallelism).
The memory allowances above are Kernel Manager policy, not prescribed by those manuals.
