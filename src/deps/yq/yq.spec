Name:           yq
Version:        4.53.6
Release:        1%{?dist}
Summary:        Portable command-line YAML, JSON, XML, CSV, TOML, HCL, and properties processor
License:        MIT
URL:            https://github.com/mikefarah/yq

# Repackage the official, architecture-specific release archive.  The
# checksums are verified in %prep because the upstream assets are not Fedora
# lookaside sources.
%ifarch x86_64
Source0:        https://github.com/mikefarah/yq/releases/download/v%{version}/yq_linux_amd64.tar.gz
%global yq_binary yq_linux_amd64
%global yq_tarball_sha512 8ad4eeb1a8fca8ab0b899e3585232f6f92b17bb723edca9ed5856d1adb9b77cb9a723e515ccffb66bf4f687cca9f1f13d6d98102e64c64b53942feccb3c3760c
%endif
%ifarch aarch64
Source0:        https://github.com/mikefarah/yq/releases/download/v%{version}/yq_linux_arm64.tar.gz
%global yq_binary yq_linux_arm64
%global yq_tarball_sha512 99541a63b921903918f8b769cb5dbcf78663ce15be001b820ab30503736b138dd52f30ce5d13aa8c576cbf0399554756613aff2feeadedace642f14489f43834
%endif
Source1:        https://raw.githubusercontent.com/mikefarah/yq/v%{version}/LICENSE

ExclusiveArch:  x86_64 aarch64

%description
Yq is a portable command-line processor for YAML, JSON, XML, CSV, TOML, HCL,
and properties files.  This package provides the Go implementation from
mikefarah/yq, including the -o=json output mode and strenv operator.

%prep
echo "%{yq_tarball_sha512}  %{SOURCE0}" | sha512sum -c -
tar -xzf %{SOURCE0}
install -pm 0755 %{yq_binary} yq

%build
./yq shell-completion bash > yq.bash
./yq shell-completion fish > yq.fish
./yq shell-completion zsh > yq.zsh

%install
install -Dpm 0755 yq %{buildroot}%{_bindir}/yq
install -Dpm 0644 yq.1 %{buildroot}%{_mandir}/man1/yq.1
install -Dpm 0644 yq.bash %{buildroot}%{_datadir}/bash-completion/completions/yq
install -Dpm 0644 yq.fish %{buildroot}%{_datadir}/fish/vendor_completions.d/yq.fish
install -Dpm 0644 yq.zsh %{buildroot}%{_datadir}/zsh/site-functions/_yq
install -Dpm 0644 %{SOURCE1} %{buildroot}%{_datadir}/licenses/%{name}/LICENSE

%check
case "$(./yq --version)" in
    *"version v%{version}") ;;
    *) echo "unexpected yq version: $(./yq --version)" >&2; exit 1 ;;
esac
printf '%s\n' 'name: expected' > yq-test.yml
result="$(NAME=expected ./yq -o=json '.name == strenv(NAME)' yq-test.yml)"
test "$result" = true

%files
%license %{_datadir}/licenses/%{name}/LICENSE
%{_bindir}/yq
%{_mandir}/man1/yq.1*
%{_datadir}/bash-completion/completions/yq
%{_datadir}/fish/vendor_completions.d/yq.fish
%{_datadir}/zsh/site-functions/_yq

%changelog
* Sat Oct 17 2026 TunaOS Contributors <packages@tunaos.org> - 4.53.6-1
- Package the Go implementation from the official, checksum-locked release archives.
