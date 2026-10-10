#!/usr/bin/env bats
# BATS tests for scripts/arch-clean-install.sh — Arch clean-install harness.
#
# The whole point of this harness is to prove that the package THIS JOB BUILT
# installs and resolves. pacman resolves `-S <name>` by walking the sync
# repositories in configuration order and taking the first that provides the
# name; it does not compare versions across them. So if [tideforge] is not
# listed ahead of [core]/[extra], every package name that also exists in an
# official Arch repository is installed from Arch, and the built artifact is
# never exercised.
#
# That regression is silent by construction — the job still goes green, just
# against the wrong package. Run 31113235209 built bazaar 0.9.1-1 and reported
# `bazaar 0.9.2-1`; niri, greetd and dgop all exist in extra too and were
# passing the same way. So the ordering is pinned here rather than left to
# review.

REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../.." && pwd)"
SCRIPT="${REPO_ROOT}/scripts/arch-clean-install.sh"

# Extract the repository section names, in order, from the pacman.conf the
# script generates. Parsing the emitted config rather than grepping the source
# means a refactor that still produces the right file keeps passing, and one
# that produces the wrong file fails even if the source text looks fine.
emitted_repo_order() {
	local block
	# Execute only the actual configuration renderer. Manager/key/mirror setup
	# stays outside this fixture; native policy has its own behavior tests.
	block=$(sed -n '/^{/,/^} > \/tmp\/tideforge-pacman.conf/p' "$SCRIPT" |
		sed 's#^} > /tmp/tideforge-pacman.conf$#}#')
	native_repositories=$'core\nextra\nalarm\naur' bash -c "$block" |
		sed -n 's/^\[\([a-z]*\)\]$/\1/p'
}

@test "arch-clean-install.sh: exists" {
	run test -f "$SCRIPT"
	[ "$status" -eq 0 ]
}

@test "arch-clean-install.sh: has bash shebang" {
	run head -1 "$SCRIPT"
	[[ "$output" =~ ^#!/.*bash ]]
}

@test "the generated pacman.conf defines options, tideforge, core and extra" {
	run emitted_repo_order
	[ "$status" -eq 0 ]
	[[ "$output" == *"options"* ]]
	[[ "$output" == *"tideforge"* ]]
	[[ "$output" == *"core"* ]]
	[[ "$output" == *"extra"* ]]
}

# The assertion this file exists for.
@test "tideforge is searched before core and extra" {
	local order tideforge core extra
	order="$(emitted_repo_order)"
	tideforge=$(echo "$order" | grep -n '^tideforge$' | cut -d: -f1)
	core=$(echo "$order" | grep -n '^core$' | cut -d: -f1)
	extra=$(echo "$order" | grep -n '^extra$' | cut -d: -f1)

	[ -n "$tideforge" ]
	[ -n "$core" ]
	[ -n "$extra" ]
	# Lower position number == searched first == wins the name.
	[ "$tideforge" -lt "$core" ]
	[ "$tideforge" -lt "$extra" ]
}

@test "native ARM repositories remain after the local candidate repository" {
	local order
	order="$(emitted_repo_order)"
	[[ "$order" == $'options\ntideforge\ncore\nextra\nalarm\naur' ]]
}

# A local repository pacman refuses to read is the same failure wearing a
# different hat: the built package cannot win a name it cannot be loaded from.
@test "the tideforge repository stays installable with unsigned CI artifacts" {
	run grep -A2 "echo '\[tideforge\]'" "$SCRIPT"
	[ "$status" -eq 0 ]
	[[ "$output" == *"SigLevel = Optional TrustAll"* ]]
	[[ "$output" == *"Server = file:///var/lib/tideforge"* ]]
}

# `pacman -Q` after the install is what turned the 0.9.1-vs-0.9.2 substitution
# from invisible into evidence. It is load-bearing, not decoration.
@test "the install is proven with pacman -Q afterwards" {
	run grep -E '^pacman -Q "\$package"' "$SCRIPT"
	[ "$status" -eq 0 ]
}
