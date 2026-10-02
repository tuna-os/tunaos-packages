#!/usr/bin/env python3
import os

import yaml

# GitHub caps a matrix at 256 jobs per workflow run. Tiering once over every
# desktop makes tiers much wider than the per-desktop orders ever were --
# layer-15 alone is 251 packages, five under the cap -- so a tier that outgrows
# it is split across several matrix jobs instead of failing to schedule.
#
# Splitting costs nothing: a tier's packages have no edges between them (that
# is what being in one tier means), so the chunks are siblings sharing one
# `needs` and one consolidate barrier, not extra rounds.
MAX_MATRIX_JOBS = 250


# Every rclone in this workflow moves the same shape of data: a few thousand
# small RPMs between R2 and a runner. rclone's defaults (4 transfers, 8
# checkers, paginated listing) are tuned for a handful of large files, so the
# seed spends its time on per-object round trips rather than on bandwidth.
#
# --stats is here so the next person can see the split between the transfer and
# the createrepo_c that follows it; the first fan-out logged 108 silent seconds
# and no way to tell which half to attack.
RCLONE_FLAGS = "--transfers 32 --checkers 64 --fast-list --stats 30s --stats-one-line"


def _rclone_conf(r2_state):
    """One rclone endpoint for the whole workflow.

    The seed and publish jobs built theirs from CLOUDFLARE_ACCOUNT_ID while
    the per-tier jobs use R2_ENDPOINT. Two secrets for one endpoint means
    whichever is unset fails at runtime, two minutes in, in a job that looks
    unrelated to the change that introduced it. --r2-state makes every job
    use R2_ENDPOINT, which is the secret the existing Hummingbird workflow
    has been publishing with.
    """
    endpoint = ('${{ secrets.R2_ENDPOINT }}' if r2_state
                else 'https://${{ secrets.CLOUDFLARE_ACCOUNT_ID }}.r2.cloudflarestorage.com')
    return ('mkdir -p ~/.config/rclone\numask 077\ncat > ~/.config/rclone/rclone.conf << EOF\n'
            '[r2]\ntype = s3\nprovider = Cloudflare\n'
            'access_key_id = ${{ secrets.R2_ACCESS_KEY_ID }}\n'
            'secret_access_key = ${{ secrets.R2_SECRET_ACCESS_KEY }}\n'
            f'endpoint = {endpoint}\nEOF\n')


def generate_workflow(manifest_path, output_path, workflow_name='Distributed Build and Publish RPMs',
                      r2_path='repo/10-stream-x86_64', secondary_r2_path='repo/10-x86_64',
                      install_script='contrib/install.sh', install_r2_dest='install.sh',
                      submodules=True, mock_config=None, r2_state=False):
    with open(manifest_path) as f:
        manifest = yaml.safe_load(f)

    # Allow manifest to specify r2_path; CLI arg takes precedence if explicitly set
    manifest_r2_path = manifest.get('r2_path', r2_path)
    if r2_path == 'repo/10-stream-x86_64' and manifest_r2_path != 'repo/10-stream-x86_64':
        r2_path = manifest_r2_path

    tiers = manifest.get('tiers', [])
    manifest_filename = os.path.basename(manifest_path)

    workflow = {
        'name': workflow_name,
        # Least privilege: no job here uses GITHUB_TOKEN beyond checkout
        # (artifacts and cache use the runtime token, R2 uses its own secrets).
        'permissions': {'contents': 'read'},
        'on': {
            'workflow_dispatch': {
                'inputs': {
                    'force': {
                        'description': 'Force rebuild even if package exists in repo',
                        'type': 'boolean',
                        'default': False
                    }
                }
            }
        },
        'env': {
            'R2_BUCKET': 'bluefin',
            'LOCAL_REPO': '${{ github.workspace }}/local-repo',
            'MOCK_RUNNER_IMAGE': 'ghcr.io/tuna-os/mock-runner:centos-stream-10'
        },
        'jobs': {}
    }
    
    jobs = workflow['jobs']
    
    # 1. Seed Repo Job
    jobs['seed-repo'] = {
        'runs-on': 'ubuntu-latest',
        'steps': [
            {'name': 'Checkout', 'uses': 'actions/checkout@v7'},
            {'name': 'Install dependencies', 'run': 'sudo apt-get update -q || sudo apt-get update -q || true\nsudo apt-get install -y -q createrepo-c rclone'},
            {'name': 'Configure rclone', 'run': _rclone_conf(r2_state)},
            {'name': 'Seed local repo from R2', 'run': f'mkdir -p local-repo\necho "Downloading existing repo from R2..."\nrclone sync "r2:${{R2_BUCKET}}/{r2_path}/" "local-repo/" --exclude "repodata/**" {RCLONE_FLAGS} || true\ncreaterepo_c local-repo\n'},
            # With R2 as the shared state this job exists to guarantee the
            # repository has valid metadata before any runner seeds from it;
            # there is nothing to hand on as an artifact.
            *([] if r2_state else [
                {'name': 'Upload initial repo', 'uses': 'actions/upload-artifact@v7', 'with': {'name': 'repo-0', 'path': 'local-repo', 'retention-days': 1}},
            ])
        ]
    }
    
    prev_tier_repo = 'repo-0'
    prev_consolidate_job = 'seed-repo'
    
    # Build --manifest flag for build-chain.sh (only needed for non-default manifest)
    manifest_flag = f' --manifest {manifest_filename}' if manifest_filename != 'build-order.yml' else ''
    # Chroot selection. GNOME 49 builds in build-chain.sh's default chroot,
    # but GNOME 50 has its own mock config (extra repos, tmpfs opts), and a
    # generated workflow that silently built in the wrong chroot would only
    # fail once a package needed one of those repos. The cache keys hash the
    # SELECTED config, not the default one, for the same reason.
    mock_flag = f' --mock-config {mock_config}' if mock_config else ''
    mock_cfg_file = f'mock/{mock_config}.cfg' if mock_config else 'mock/centos-stream-10-ci.cfg'

    for i, tier in enumerate(tiers):
        tier_name = tier['name']
        packages = [pkg['path'] for pkg in tier['packages'] if 'path' in pkg]
        
        consolidate_job_name = f'consolidate-{tier_name}'
        repo_artifact_name = f'repo-{i+1}'

        chunks = [packages[k:k + MAX_MATRIX_JOBS]
                  for k in range(0, len(packages), MAX_MATRIX_JOBS)] or [[]]
        build_job_names = [
            f'build-{tier_name}' if len(chunks) == 1 else f'build-{tier_name}-{k:02d}'
            for k in range(len(chunks))
        ]

        # Build Jobs: one per chunk, all siblings of the same barrier.
        for chunk_index, (build_job_name, chunk) in enumerate(zip(build_job_names, chunks)):
            jobs[build_job_name] = {
                'needs': prev_consolidate_job,
                # Same reasoning, one link further down: seed-repo is the only
                # predecessor whose failure should stop everything, and it is
                # the predecessor of the first tier only.
                **({} if prev_consolidate_job == 'seed-repo'
                   else {'if': '${{ !cancelled() }}'}),
                'runs-on': 'ubuntu-latest',
                'strategy': {
                    'fail-fast': False,
                    'matrix': {
                        'package': chunk
                    }
                },
                'steps': [
                    {'name': 'Checkout', 'uses': 'actions/checkout@v7', **({'with': {'submodules': 'recursive'}} if submodules else {})},
                    {'name': 'Install host dependencies', 'run': 'sudo apt-get update -q || sudo apt-get update -q || true\nsudo apt-get install -y -q podman createrepo-c rpm rclone'},
                    {'name': 'Cache CentOS Stream 10 image', 'uses': 'actions/cache@v6', 'with': {'path': '/tmp/cs10-image.tar', 'key': f"cs10-image-${{{{ hashFiles('{mock_cfg_file}') }}}}"}},
                    {'name': 'Load or pull image', 'run': 'if [[ -f /tmp/cs10-image.tar ]]; then\n  podman load -i /tmp/cs10-image.tar\nelse\n  podman pull quay.io/centos/centos:stream10\n  podman save -o /tmp/cs10-image.tar quay.io/centos/centos:stream10\nfi\npodman pull ${{ env.MOCK_RUNNER_IMAGE }}\n'},
                    *([
                        # Seed each runner straight from R2 rather than passing the
                        # whole repository between tiers as an artifact.
                        #
                        # The artifact route is O(repo x packages-in-tier): every
                        # runner downloads the entire repo. Hummingbird's repo is
                        # over a gigabyte and gnome-00 alone is 116 packages, so a
                        # single tier would move on the order of a hundred gigabytes
                        # of artifact traffic to distribute a few hundred megabytes
                        # of RPMs. Seeding is O(1) per runner and takes about two
                        # minutes, which the sequential workflow already pays once.
                        #
                        # Correctness is unchanged because the barrier is unchanged:
                        # a tier's runners start only after the previous tier's
                        # consolidate job has published to R2.
                        {'name': 'Seed local repo from R2', 'run': ('mkdir -p ~/.config/rclone\numask 077\ncat > ~/.config/rclone/rclone.conf << EOF\n[r2]\ntype = s3\nprovider = Cloudflare\naccess_key_id = ${{ secrets.R2_ACCESS_KEY_ID }}\nsecret_access_key = ${{ secrets.R2_SECRET_ACCESS_KEY }}\nendpoint = ${{ secrets.R2_ENDPOINT }}\nEOF\nmkdir -p local-repo\nrclone copy "r2:${R2_BUCKET}/%s/" local-repo/ ' + RCLONE_FLAGS + ' || true\ncreaterepo_c local-repo\n') % r2_path},
                    ] if r2_state else [
                        {'name': 'Download previous repo', 'uses': 'actions/download-artifact@v8', 'with': {'name': prev_tier_repo, 'path': 'local-repo'}},
                    ]),
                    # /var/cache/mock is dnf's download cache plus the chroot
                    # root cache. Both are keyed by the mock config, not by the
                    # package being built -- that is what makes them shareable.
                    #
                    # Keying per package came from Tideforge, where ~46 leaf
                    # packages are rebuilt over and over and a package's own
                    # cache is exactly what you want. A fan-out inverts that:
                    # each of 1248 packages builds ONCE, so a per-package key
                    # cannot hit inside a run, and every job still writes an
                    # entry. Measured on run 31294475023, one job wrote
                    #
                    #     Sent 372724132 of 372724132 (100.0%), 220.6 MBs/sec
                    #
                    # 372 MB x 1248 jobs is about 465 GB of writes against a
                    # 10 GB per-repository cache limit. It evicts continuously,
                    # so the restore-keys almost never hit, and it takes the
                    # cs10-image entry -- which does hit -- down with it.
                    #
                    # Dropping the package from the key means one entry per run
                    # instead of 1248. Concurrent jobs racing to save the same
                    # key is fine: the first wins and the rest warn.
                    {'name': 'Cache mock chroot (dnf downloads)', 'uses': 'actions/cache@v6', 'with': {'path': '.mock-cache', 'key': (f"mock-cache-${{{{ hashFiles('{mock_cfg_file}') }}}}-${{{{ github.run_id }}}}" if r2_state else f"mock-cache-${{{{ matrix.package }}}}-${{{{ hashFiles('{mock_cfg_file}') }}}}-${{{{ github.run_id }}}}"), 'restore-keys': (f"mock-cache-${{{{ hashFiles('{mock_cfg_file}') }}}}-" if r2_state else f"mock-cache-${{{{ matrix.package }}}}-${{{{ hashFiles('{mock_cfg_file}') }}}}-\nmock-cache-${{{{ matrix.package }}}}-")}},
                    # Import this package's dist-git packaging before building it.
                    #
                    # The sequential workflow imports a whole tier in one step; a
                    # fan-out job needs exactly one package, which is also far
                    # gentler on src.fedoraproject.org than bulk parallel clones --
                    # one clone per runner rather than four at a time over hundreds.
                    #
                    # Without this the runner has no spec at all and build-chain
                    # dies in find_spec with "package: <none in scope>", which is
                    # what happened on the first dispatch (run 31287886482): all
                    # three bootstrap-00 jobs failed in under three seconds.
                    #
                    # Packages under src/deps are maintained in this repository and
                    # carry no distgit key, so their spec is already checked out.
                    # The existence test is what tells the two apart.
                    {'name': "Import this package's dist-git packaging", 'run': 'set -euo pipefail\npkg_path="${{ matrix.package }}"\nif [ -d "$pkg_path" ]; then\n  echo "$pkg_path is maintained in-tree; nothing to import"\nelse\n  python3 scripts/import-fedora-distgit.py \\\n    --package "$(basename "$pkg_path")" \\\n    --dest "$(dirname "$pkg_path")" \\\n    --branch rawhide --release-bump\nfi\n'},
                    {'name': 'Build package', 'env': {'MOCK_CACHE_DIR': '${{ github.workspace }}/.mock-cache'}, 'run': f'touch .build-marker\nARGS=(--backend podman --tier {tier_name} --package ${{{{ matrix.package }}}}{manifest_flag}{mock_flag})\nif [[ "${{{{ github.event.inputs.force }}}}" == "true" ]]; then\n  ARGS+=(--force)\nfi\n./scripts/build-chain.sh "${{ARGS[@]}}"\n'},
                    {'name': 'Find new RPMs', 'id': 'find-rpms', 'run': 'mkdir -p new-rpms\nfind local-repo -name "*.rpm" -newer .build-marker -exec cp {} new-rpms/ \\;\ncount=$(ls -1 new-rpms/*.rpm 2>/dev/null | wc -l)\necho "count=$count" >> $GITHUB_OUTPUT\n'},
                    {'name': 'Upload RPMs', 'if': "steps.find-rpms.outputs.count > '0'", 'uses': 'actions/upload-artifact@v7', 'with': {'name': f'rpms-{tier_name}-{chunk_index}-${{{{ strategy.job-index }}}}', 'path': 'new-rpms/*.rpm', 'retention-days': 1}}
                ]
            }
        
        # Consolidate Job
        jobs[consolidate_job_name] = {
            'needs': build_job_names[0] if len(build_job_names) == 1 else build_job_names,
            # A tier's job fails if ANY of its packages failed, and `needs`
            # treats that as a stop sign: the consolidate is skipped, so the
            # next tier is skipped, so every remaining tier and the publish
            # are skipped too. One bad package out of 1248 would throw away
            # the whole run and publish nothing.
            #
            # Rebuilding 1248 Rawhide packages will always turn up some that
            # do not build -- SwayNotificationCenter BuildRequires
            # pkgconfig(granite-7) and Rawhide ships Granite 6. So the barrier
            # publishes what did build and the chain carries on. Packages
            # downstream of a failure fail on their own missing dependency,
            # which is the report you want: every failure in one pass instead
            # of one per re-dispatch.
            #
            # The run still ends red -- a run's conclusion is failure if any
            # job failed, whatever the later jobs do.
            'if': '${{ !cancelled() }}',
            'runs-on': 'ubuntu-latest',
            'steps': [
                {'name': 'Install createrepo_c', 'run': 'sudo apt-get update -q || sudo apt-get update -q || true\nsudo apt-get install -y -q createrepo-c rclone'},
                *([] if r2_state else [
                    {'name': 'Download previous repo', 'uses': 'actions/download-artifact@v8', 'with': {'name': prev_tier_repo, 'path': 'local-repo'}},
                ]),
                # Only this tier's NEW rpms travel as artifacts -- megabytes,
                # not the whole repository.
                {'name': 'Download new RPMs', 'uses': 'actions/download-artifact@v8', 'continue-on-error': True, 'with': {'pattern': f'rpms-{tier_name}-*', 'path': 'local-repo', 'merge-multiple': True}},
                *([
                    # copy, not sync: this tier adds to what earlier tiers
                    # published and must never delete it.
                    {'name': 'Publish this tier to R2', 'run': ('mkdir -p ~/.config/rclone\numask 077\ncat > ~/.config/rclone/rclone.conf << EOF\n[r2]\ntype = s3\nprovider = Cloudflare\naccess_key_id = ${{ secrets.R2_ACCESS_KEY_ID }}\nsecret_access_key = ${{ secrets.R2_SECRET_ACCESS_KEY }}\nendpoint = ${{ secrets.R2_ENDPOINT }}\nEOF\nrclone copy local-repo/ "r2:${R2_BUCKET}/%s/" ' + RCLONE_FLAGS + '\n') % r2_path},
                ] if r2_state else [
                    {'name': 'Update repo', 'run': 'createrepo_c --update local-repo'},
                    {'name': 'Upload updated repo', 'uses': 'actions/upload-artifact@v7', 'with': {'name': repo_artifact_name, 'path': 'local-repo', 'retention-days': 1}},
                ])
            ]
        }
        
        prev_tier_repo = repo_artifact_name
        prev_consolidate_job = consolidate_job_name

    # Final Publish Job
    jobs['publish'] = {
        'needs': prev_consolidate_job,
        # Sign and publish whatever the run did build.
        'if': '${{ !cancelled() }}',
        'runs-on': 'ubuntu-latest',
        'steps': [
            {'name': 'Checkout', 'uses': 'actions/checkout@v7'},
            {'name': 'Install dependencies', 'run': 'sudo apt-get update -q || sudo apt-get update -q || true\nsudo apt-get install -y -q rpm createrepo-c gpg gpgconf rclone'},
            *([
                {'name': 'Seed final repo from R2', 'run': ('mkdir -p ~/.config/rclone\numask 077\ncat > ~/.config/rclone/rclone.conf << EOF\n[r2]\ntype = s3\nprovider = Cloudflare\naccess_key_id = ${{ secrets.R2_ACCESS_KEY_ID }}\nsecret_access_key = ${{ secrets.R2_SECRET_ACCESS_KEY }}\nendpoint = ${{ secrets.R2_ENDPOINT }}\nEOF\nmkdir -p local-repo\nrclone copy "r2:${R2_BUCKET}/%s/" local-repo/ ' + RCLONE_FLAGS + '\n') % r2_path},
            ] if r2_state else [
                {'name': 'Download final repo', 'uses': 'actions/download-artifact@v8', 'with': {'name': prev_tier_repo, 'path': 'local-repo'}},
            ]),
            {'name': 'Import GPG key', 'id': 'import-gpg', 'uses': 'crazy-max/ghaction-import-gpg@v7', 'with': {'gpg_private_key': '${{ secrets.GPG_PRIVATE_KEY }}', 'passphrase': '${{ secrets.GPG_PASSPHRASE }}', 'git_committer_name': 'RPM Builder', 'git_committer_email': 'rpm-signing@tunaos.org'}},
            {'name': 'Configure RPM macros', 'run': 'echo "%_signature gpg" > ~/.rpmmacros\necho "%_gpg_name ${{ steps.import-gpg.outputs.keyid }}" >> ~/.rpmmacros\necho "%__gpg_sign_cmd %{__gpg} gpg --batch --no-verbose --no-armor --use-agent --no-secmem-warning -u \\"%{_gpg_name}\\" -sbo %{__signature_filename} %{__plaintext_filename}" >> ~/.rpmmacros\n'},
            {'name': 'Sign RPMs', 'run': 'find local-repo -name "*.rpm" -exec rpmsign --addsign {} \\;\ncreaterepo_c --update local-repo\n'},
            # The upload below is `rclone sync`, which makes the served prefix
            # MATCH local-repo. A publisher that re-indexes without signing
            # therefore does not merely skip a step -- it DELETES the
            # repomd.xml.asc that publish-rpm-wave.sh wrote into the same
            # prefix, and repo_gpgcheck=1 can never be turned on. Signing here
            # is what keeps the metadata signature durable across publishers.
            # See #509.
            {'name': 'Sign repository metadata', 'run': 'bash scripts/sign-repomd.sh local-repo\n'},
            {'name': 'Configure rclone', 'run': _rclone_conf(r2_state)},
            {'name': 'Upload to R2', 'run': _build_upload_run(r2_path, secondary_r2_path, install_script, install_r2_dest)}
        ]
    }

    class IndentedDumper(yaml.Dumper):
        def increase_indent(self, flow=False, indentless=False):
            return super().increase_indent(flow, False)

    # Write yaml out without using standard aliases and formatting correctly
    with open(output_path, 'w') as f:
        yaml.dump(workflow, f, Dumper=IndentedDumper, default_flow_style=False, sort_keys=False, width=float("inf"))


def _build_upload_run(r2_path, secondary_r2_path, install_script, install_r2_dest):
    # Use just the last path component for the echo message
    label = r2_path.split('/')[-1]
    lines = [f'echo "Uploading to {label}..."',
             f'rclone sync local-repo/ "r2:${{R2_BUCKET}}/{r2_path}/" ' + RCLONE_FLAGS]
    if secondary_r2_path:
        label2 = secondary_r2_path.split('/')[-1]
        lines += [f'echo "Uploading to {label2}..."',
                  f'rclone sync local-repo/ "r2:${{R2_BUCKET}}/{secondary_r2_path}/" ' + RCLONE_FLAGS]
    lines += ['rclone copyto public.gpg "r2:${R2_BUCKET}/public.gpg"',
              f'rclone copyto {install_script} "r2:${{R2_BUCKET}}/{install_r2_dest}"']
    return '\n'.join(lines) + '\n'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Generate distributed build workflow from manifest')
    parser.add_argument('manifest', nargs='?',
                        default=os.path.join(os.path.dirname(__file__), '..', 'build-order.yml'),
                        help='Path to build-order YAML manifest')
    parser.add_argument('output', nargs='?',
                        default=os.path.join(os.path.dirname(__file__), '..', '.github', 'workflows', 'build-distributed.yml'),
                        help='Output workflow YAML path')
    parser.add_argument('--name', default='Distributed Build and Publish RPMs',
                        help='Workflow name')
    parser.add_argument('--r2-path', default='repo/10-stream-x86_64',
                        help='Primary R2 repo path (e.g. gnome49/10-stream-x86_64)')
    parser.add_argument('--secondary-r2-path', default='repo/10-x86_64',
                        help='Secondary R2 path (GNOME 50 only; set empty to disable)')
    parser.add_argument('--install-script', default='contrib/install.sh',
                        help='Install script to upload to R2')
    parser.add_argument('--install-r2-dest', default='install.sh',
                        help='R2 destination key for install script')
    parser.add_argument('--no-submodules', action='store_true',
                        help='Skip submodules: recursive in checkout steps')
    parser.add_argument('--mock-config', default=None,
                        help='Mock config name passed to build-chain.sh (e.g. centos-stream-10-ci-gnome50); also keys the image/chroot caches on that config file')
    parser.add_argument('--r2-state', action='store_true',
                        help='Carry the repository between tiers in R2 rather than as a GitHub '
                             'artifact. Each build runner seeds itself and each tier publishes '
                             'its own rpms. Required at Hummingbird scale: the artifact route '
                             'makes every runner download the whole repository, which is over a '
                             'gigabyte, so one 116-package tier would move ~116 GB to distribute '
                             'a few hundred megabytes.')
    args = parser.parse_args()
    secondary = args.secondary_r2_path if args.secondary_r2_path else None
    generate_workflow(args.manifest, args.output,
                      workflow_name=args.name,
                      r2_path=args.r2_path,
                      secondary_r2_path=secondary,
                      install_script=args.install_script,
                      install_r2_dest=args.install_r2_dest,
                      submodules=not args.no_submodules,
                      mock_config=args.mock_config,
                      r2_state=args.r2_state)
    print(f"Generated {args.output}")
