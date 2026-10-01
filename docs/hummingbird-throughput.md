# Where Hummingbird desktop build time goes

Data comes from GitHub Actions logs of five `Build Hummingbird desktops` runs between 2026-07-25 and 2026-08-08.
Per-package durations come from mock log lines.
Wall clock measurements come from the start and end timestamps of the build chain.

Method note: `build-chain.sh` runs each package in a background subshell.
The stdout stream is a pipe, so a worker output block carries the exit timestamp of that worker.
Per-package durations must come from mock timers instead of overall log timestamps.

## The runs

| run | tier(s) | packages built | Σ mock | build-step wall | Σ mock / wall |
|---|---|---|---|---|---|
| [31137480986](https://github.com/tuna-os/tunaos-packages/actions/runs/31137480986) | gnome-00 | 106 | 282.0 m | 288.4 m | **97.8%** |
| [31158689244](https://github.com/tuna-os/tunaos-packages/actions/runs/31158689244) | gnome-00 | 106 | 240.5 m | 246.2 m | **97.7%** |
| [31179614825](https://github.com/tuna-os/tunaos-packages/actions/runs/31179614825) | gnome-01 | 29 | 75.7 m | 77.5 m | **97.6%** |
| [31215339645](https://github.com/tuna-os/tunaos-packages/actions/runs/31215339645) | kde-00 | 47 | 87.6 m | 90.3 m | **97.0%** |
| [31242725235](https://github.com/tuna-os/tunaos-packages/actions/runs/31242725235) | niri-00 | 12 | 15.7 m | 16.4 m | **95.6%** |

The corpus contains 194 distinct packages and 6.80 hours of mock time.
Values are: min 42 s, p10 52 s, median 77.5 s, mean 126 s, p90 186 s, max 2686 s.

## Item 1 -- the job runs at concurrency 1.0, not 2

Total mock time is 95.6% to 97.8% of the wall clock time across all runs with `--jobs 2`.
Two workers cannot both run mock 97% of the time simultaneously.
The lock on `repo.lock` around `mock --rebuild` in `build_package_podman` is exclusive.
Therefore `--jobs` sets how many workers wait in the queue.
PR #266 diagnosed this behavior from code, and these numbers confirm it.

Other optimizations in the build step yield no benefit until the lock splits readers and writers.
At concurrency 1, improvements in job schedules remain ineffective.

## Item 2 -- 34% of mock time rebuilds the root cache 194 times

The line `Start: creating root cache` appears once per package across all five runs.
The string `unpacking root cache` does not appear in logs.
The workflow does not mount `/var/cache/mock` because `MOCK_CACHE_DIR` was unset.
Podman runs with `--rm`, so the system deletes the cache directory at container exit.
Every package installed the minimal buildroot through dnf5, then compressed a root cache that no subsequent run used.

The shortest mock run observed is 42 s (`python-aiohappyeyeballs`, which stopped at `%pyproject_buildrequires`).
The shortest successful build is 46 s (`vpnc-script`, which installs one shell script).
The eight `niri-00` packages that stopped at `%pyproject_buildrequires` ran in 42 to 52 s.

A baseline of 43 s repeated 194 times wastes **2.32 hours out of 6.80 hours (34.1%)**.

Mock avoids this overhead by design, and `--uniqueext` does not disable the cache:

```python
self.shared_root_name = config['root']
if 'unique-ext' in config:
    config['root'] = "%s-%s" % (config['root'], config['unique-ext'])
...
self.cachedir = os.path.join(self.cache_topdir, self.shared_root_name)
```

The cache uses the base name before it appends `unique-ext`.
Each package chroot shares a single cache through an `fcntl` lock in `plugins/root_cache.py`.
On a cache hit, `_init()` finds the chroot populated and skips initialization.
`_rebuild_root_cache()` then declines to create a new archive.

This mechanism is safe when the local repository changes between tiers.
The root cache holds only the minimal buildroot.
`BuildRequires` resolve against live repositories after the unpack of the root.
The template sets `metadata_expire=0` so mock revalidates metadata on every transaction.

## Item 3 -- the longest package takes 11% of time, not 93%

The largest package in the corpus is `highway` at 44.8 minutes, followed by `abseil-cpp` at 30.0 minutes.
`highway` accounts for 16% of mock time in `gnome-00` and 11% of wall clock time for the tier.

This distribution determines how much parallelism helps.
For `gnome-00` (sum 16921 s, max 2707 s), wall clock time with W workers equals `max(sum/W, max_pkg)`:

| W | 1 | 2 | 4 | 6 | 8 | 16 |
|---|---|---|---|---|---|---|
| gnome-00 wall | 282 m | 141 m | 70 m | 47 m | 45 m | 45 m |

The long build binds when W >= 7.
Below that, work distribution is efficient.
At W=4, the tier boundary causes no measurable loss.

## Consequences for issue #267

1. **Tier barriers:**
   On 4 vCPU runners, the idle tail of a tier is near zero.
   A DAG wavefront helps only above six concurrent builds per tier, which needs both #266 and additional CPU cores.

2. **One runner per dispatch:**
   680 packages at 126 s average needs **~23.8 hours of serial mock execution**.
   No single runner can finish this work within the 360-minute limit for jobs.

3. **Runner size:**
   The repository uses standard `ubuntu-latest` and `ubuntu-24.04-arm` runners.
   Four concurrent mock builds share four cores with `%{_smp_mflags}`.
   For compilation-heavy packages (`highway`, `abseil-cpp`), adding runners provides more speed than adding concurrency inside one runner.

## Runner budget

Each desktop job needs **5 concurrent runners** for `desktop: all`, plus a short initial step.
This uses 8% of the organization pool of 60 runners.
The dispatch workflow does not block scheduled cron runs.

Each desktop job rebuilds the bootstrap tiers (10 packages, ~10 minutes).
This duplicates 40 runner-minutes of work without delay to the main path.
The project did not shard tiers because each job adds 184 seconds of setup overhead (checkout, packages, podman, R2).
To split all 680 packages across jobs would spend ~35 runner-hours on setup to parallelize ~24 runner-hours of builds.

## Canary comparison and the root cache fix

Tier `niri-00`, 24 packages, `force: true`, `publish: false`, with #266 applied (`--jobs $(nproc)` = 4):

| run | branch | build-chain wall | Σ mock | `creating root cache` | `unpacking root cache` |
|---|---|---|---|---|---|
| [31265993115](https://github.com/tuna-os/tunaos-packages/actions/runs/31265993115) | `main` | **39.02 m** | 74.2 m | 24 | 0 |
| [31268488082](https://github.com/tuna-os/tunaos-packages/actions/runs/31268488082) | + `MOCK_CACHE_DIR` | **39.49 m** | — | 24 | 0 |

The mount succeeded, but mock rebuilt the cache 18 times:

```text
INFO: /tmp/mock-configdir/hummingbird-ci.cfg newer than root cache; cache will be rebuilt
```

`_unpack_root_cache` deletes the archive when configuration files have newer timestamps.
`build-chain.sh` copied configurations without file timestamps:

```sh
cp -a /etc/mock/. /tmp/mock-configdir/
cp    /repo-mock/*.cfg /tmp/mock-configdir/
```

The second `cp` command lacked `-p`, so `hummingbird-ci.cfg` received a current timestamp.
Mock detected the configuration as modified, deleted the cache, and rebuilt the buildroot for every package.

The `-p` option keeps the modification time of files.
The configuration timestamp then precedes the cache archive, which allows cache hits.

Concurrency reached 74.2 m of mock over 39.02 m wall time (1.90x), below the target of 4.
The duration of each mock build doubled because four builds shared four cores.

## Postscript (2026-08-25) -- cache restoration

PR #277 added the `-p` fix and shared cache mount.
Commit `6d4b77a` removed those settings with the hummingbird pipeline (#517).
`package-factory-cell.yml` omitted `MOCK_CACHE_DIR`, which returned the cache rebuild penalty.

PR #512 restored the cache with two updates:

- The mount uses `<config>/root_cache` instead of all of `/var/cache/mock`.
  This prevents disk exhaustion from `yum_cache` during multi-hour runs.
- The workflow uses `runner.temp` instead of `actions/cache` to reduce network traffic.

`tests/test_the_mock_root_cache_is_actually_shared.py` tests against these silent failures.

## Summary Status (#267)

- **One runner per dispatch**: Matrix jobs implemented in PR #277.
- **Mock cache**: Shared cache directory with `-p` flag restored in PR #512.
- **Runner size**: Standard 4-vCPU runners across the organization.
- **DAG wavefront**: Documented for future concurrency improvements.
