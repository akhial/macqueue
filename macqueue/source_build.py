"""VPS-side preparation only: fetch/vendor dependencies without executing project code."""
import os
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from .common import digest, require
from .sources import lock_identity, package


def cargo_binary():
    executable = shutil.which('cargo')
    require(executable, 'Cargo must be installed on the VPS to vendor dependencies')
    if Path(executable).resolve().name == 'rustup':
        executable = subprocess.check_output(
            [str(Path(executable).with_name('rustup')), 'which', '--toolchain', 'stable', 'cargo'], text=True).strip()
    return Path(executable).absolute()


def build_package(bundle, project, revisions, destination, cache, cargo=None):
    require(not destination.exists(), 'package output already exists')
    cargo = cargo or cargo_binary()
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not any((cache / name).exists() for name in ('credentials', 'credentials.toml', 'config', 'config.toml')),
            'provisioning Cargo cache must not contain credentials or configuration')
    with tempfile.TemporaryDirectory(prefix='.macqueue-vendor-', dir=destination.parent) as temporary:
        root = Path(temporary).resolve()
        for name in ('home', 'tmp', 'checkouts', 'vendor'):
            (root / name).mkdir()
        env = {'PATH': str(cargo.parent) + ':/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(root / 'home'),
               'TMPDIR': str(root / 'tmp') + '/', 'CARGO_HOME': str(cache.resolve()),
               'RUSTC': str(cargo.with_name('rustc')), 'RUSTDOC': str(cargo.with_name('rustdoc')),
               'RUSTFLAGS': '', 'RUSTC_WRAPPER': '', 'RUSTC_WORKSPACE_WRAPPER': '',
               'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_TERMINAL_PROMPT': '0',
               'LANG': 'C.UTF-8'}
        checkouts, locks = [], {}
        for revision in dict.fromkeys(revisions):
            checkout = root / 'checkouts' / revision
            subprocess.run(['/usr/bin/git', '-c', 'core.hooksPath=/dev/null', 'clone', '--no-checkout',
                            '--config', 'core.hooksPath=/dev/null', '--config', 'core.fsmonitor=false',
                            str(bundle), str(checkout)], check=True, env=env, capture_output=True, timeout=120)
            subprocess.run(['/usr/bin/git', '-C', str(checkout), 'checkout', '--detach', revision],
                           check=True, env=env, capture_output=True, timeout=120)
            locks[revision] = lock_identity(checkout)
            checkouts.append(checkout)
        argv = [str(cargo), 'vendor', '--locked', '--versioned-dirs', '--manifest-path', str(checkouts[0] / 'Cargo.toml')]
        for checkout in checkouts[1:]:
            argv.extend(['--sync', str(checkout / 'Cargo.toml')])
        argv.append(str(root / 'vendor'))
        result = subprocess.run(argv, cwd=root, env=env, text=True, stdout=subprocess.PIPE, timeout=900)
        require(result.returncode == 0, 'Cargo vendor failed; no package submitted')
        config = tomllib.loads(result.stdout)
        require(not config or config == {'source': {'crates-io': {'replace-with': 'vendored-sources'},
                'vendored-sources': {'directory': str(root / 'vendor')}}}, 'unexpected Cargo source configuration')
        for checkout in checkouts:
            require(lock_identity(checkout) == locks[checkout.name], 'vendoring changed a lockfile')
        manifest = {'version': 1, 'project': project, 'revisions': locks, 'bundle_sha256': digest(bundle)}
        package(bundle, root / 'vendor', manifest, destination)
        return manifest
