"""Self-contained source/dependency packages. Never install into the host mirror or toolchain."""
import gzip
import io
import json
import os
import re
import shutil
import tarfile
import time
import tomllib
from pathlib import Path

from .common import digest, json_bytes, keys, relative, require, safe_path
from .schema import MAX_INPUT_BYTES

MAX_EXPANDED_BYTES = 4 * 1024**3
MAX_FILES = 100000
MAX_SOURCE_CACHE_BYTES = 16 * 1024**3


def cargo_config(vendor):
    return '[source.crates-io]\nreplace-with = "macqueue-vendor"\n[source.macqueue-vendor]\ndirectory = ' + json.dumps(str(vendor)) + '\n'


def lock_identity(checkout):
    for name in ('.cargo/config', '.cargo/config.toml'):
        require(not safe_path(checkout, name).exists(), 'repository Cargo configuration needs operator review')
    lock = safe_path(checkout, 'Cargo.lock', exists=True)
    require(lock.is_file() and lock.stat().st_size <= 4 * 1024**2, 'invalid Cargo.lock')
    data = tomllib.loads(lock.read_text())
    for package in data.get('package', []):
        require(package.get('source') in (None, 'registry+https://github.com/rust-lang/crates.io-index'),
                'self-service provisioning supports crates.io and workspace dependencies; other sources need operator review')
    return digest(lock)


class BoundedTarInfo(tarfile.TarInfo):
    @staticmethod
    def checked(value):
        require(0 <= value.size <= MAX_INPUT_BYTES, 'invalid archive member size')
        if value.type in (tarfile.XHDTYPE, tarfile.XGLTYPE, tarfile.GNUTYPE_LONGNAME, tarfile.GNUTYPE_LONGLINK):
            require(value.size <= 65536, 'archive extension header too large')
        require(value.type != tarfile.GNUTYPE_SPARSE, 'sparse members are not allowed')
        return value

    @classmethod
    def frombuf(cls, buf, encoding, errors):
        return cls.checked(super().frombuf(buf, encoding, errors))

    @classmethod
    def _frombuf(cls, buf, encoding, errors, **kwargs):
        # Python 3.14 routes nested extension headers through _frombuf instead
        # of the public frombuf hook used by 3.11–3.13. Bound both parser paths
        # before tarfile can allocate/read the extension body.
        return cls.checked(super()._frombuf(buf, encoding, errors, **kwargs))


class LimitedReader:
    def __init__(self, stream):
        self.stream, self.total = stream, 0

    def read(self, size=-1):
        require(0 <= size <= MAX_INPUT_BYTES, 'unbounded archive read')
        data = self.stream.read(size)
        self.total += len(data)
        require(self.total <= MAX_EXPANDED_BYTES, 'expanded archive exceeds 4 GiB')
        return data


def unpack(archive, target, check=lambda: None):
    """Extract regular files ourselves; never trust tar ownership, links, or extraction filters."""
    require(archive.stat().st_size <= MAX_INPUT_BYTES, 'source archive exceeds 512 MiB')
    target.mkdir(mode=0o700)
    seen, total = set(), 0
    with gzip.open(archive, 'rb') as zipped, tarfile.open(fileobj=LimitedReader(zipped), mode='r|', tarinfo=BoundedTarInfo) as stream:
        for item in stream:
            check()
            require(len(seen) < MAX_FILES, 'too many source package members')
            name = relative(item.name.rstrip('/') if item.isdir() else item.name)
            require(name not in seen, 'duplicate source package member')
            seen.add(name)
            require(name in ('manifest.json', 'source.bundle', 'vendor') or name.startswith('vendor/'), 'unexpected source package path')
            require(item.type in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE) and not item.sparse, 'links/special files are not allowed')
            require(0 <= item.size <= MAX_INPUT_BYTES, 'invalid archive member size')
            require(not any(key.startswith('GNU.sparse') for key in item.pax_headers), 'sparse members are not allowed')
            total += item.size
            require(total <= MAX_EXPANDED_BYTES, 'source package exceeds expanded limit')
            dest = safe_path(target, name)
            if item.isdir():
                require(item.size == 0, 'directory with file data')
                dest.mkdir(parents=True, exist_ok=True, mode=0o700)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with stream.extractfile(item) as source, dest.open('xb') as out:
                    remaining = item.size
                    while remaining:
                        check()
                        data = source.read(min(1024 * 1024, remaining))
                        require(data, 'truncated archive member')
                        out.write(data)
                        remaining -= len(data)
                dest.chmod(0o700 if item.mode & 0o111 else 0o600)
    manifest_path = safe_path(target, 'manifest.json', exists=True)
    require(manifest_path.stat().st_size <= 65536, 'source manifest too large')
    manifest = json.loads(manifest_path.read_text())
    keys(manifest, ('version', 'project', 'revisions', 'bundle_sha256'))
    require(type(manifest['version']) is int and manifest['version'] == 1, 'unsupported source package')
    require(isinstance(manifest['revisions'], dict) and 1 <= len(manifest['revisions']) <= 4, 'expected 1..4 revisions')
    for commit, checksum in manifest['revisions'].items():
        require(isinstance(commit, str) and isinstance(checksum, str) and re.fullmatch(r'[a-f0-9]{40}', commit)
                and re.fullmatch(r'[a-f0-9]{64}', checksum), 'invalid revision/lock identity')
    require(digest(safe_path(target, 'source.bundle', exists=True)) == manifest['bundle_sha256'], 'Git bundle checksum mismatch')
    require(safe_path(target, 'vendor', exists=True).is_dir(), 'vendor directory missing')
    return manifest


def package(source_bundle, vendor, manifest, destination):
    paths = [source_bundle, *sorted(vendor.rglob('*'))]
    require(len(paths) < MAX_FILES and sum(p.lstat().st_size for p in paths if p.is_file()) <= MAX_EXPANDED_BYTES, 'source package exceeds limits')
    with destination.open('xb') as out, gzip.GzipFile(fileobj=out, mode='wb', filename='', mtime=0) as zipped, tarfile.open(fileobj=zipped, mode='w', format=tarfile.PAX_FORMAT) as archive:
        data = json_bytes(manifest)
        item = tarfile.TarInfo('manifest.json')
        item.size, item.mode = len(data), 0o600
        archive.addfile(item, io.BytesIO(data))
        for path in [source_bundle, vendor, *sorted(vendor.rglob('*'))]:
            require(not path.is_symlink() and (path.is_dir() or path.is_file()), 'source package contains a link/special file')
            name = 'source.bundle' if path == source_bundle else 'vendor' + ('/' + str(path.relative_to(vendor)) if path != vendor else '')
            relative(name)
            info = archive.gettarinfo(str(path), arcname=name)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ''
            if path.is_file():
                with path.open('rb') as stream:
                    archive.addfile(info, stream)
            else:
                archive.addfile(info)
    require(destination.stat().st_size <= MAX_INPUT_BYTES, 'compressed source package exceeds 512 MiB')


def catalog(policy):
    return Path(policy.config['state_dir']) / 'sources'


def lookup(policy, project, commit):
    if 'source-provision' not in policy.capabilities or 'state_dir' not in policy.config:
        return None
    root = safe_path(catalog(policy), project)
    for receipt in sorted(root.glob('*/receipt.json'), reverse=True):
        require(not receipt.is_symlink() and not receipt.parent.is_symlink(), 'source cache contains a symlink')
        value = json.loads(receipt.read_text())
        if commit in value['revisions']:
            return receipt.parent
    return None


def readonly(root):
    for path in [*root.rglob('*'), root]:
        require(not path.is_symlink(), 'source cache contains a symlink')
        path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)


def remove_snapshot(path):
    require(not path.is_symlink(), 'snapshot cannot be a symlink')
    if path.exists():
        for base, dirs, _ in os.walk(path, followlinks=False):
            Path(base).chmod(0o700)
            require(not any((Path(base) / d).is_symlink() for d in dirs), 'snapshot contains symlink')
        shutil.rmtree(path)


def publish(runner, unpacked, manifest, sha):
    root = catalog(runner.policy)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = safe_path(root, runner.spec['project'] + '/' + sha)
    if destination.exists():
        return json.loads((destination / 'receipt.json').read_text())
    require(len(list(root.glob('*/*/receipt.json'))) < 64, 'source snapshot count limit reached; revoke an unused package')
    usage = sum(p.lstat().st_size for p in root.rglob('*') if p.is_file())
    size = sum(p.stat().st_size for p in unpacked.rglob('*') if p.is_file())
    require(usage + size <= MAX_SOURCE_CACHE_BYTES, 'source cache budget reached; revoke an unused source package')
    receipt = {'project': runner.spec['project'], 'sha256': sha, 'revisions': manifest['revisions'],
               'provision_job': runner.root.name, 'verified_at': time.time(), 'offline_metadata': True}
    (unpacked / 'receipt.json').write_bytes(json_bytes(receipt))
    readonly(unpacked)
    destination.parent.mkdir(mode=0o700, exist_ok=True)
    runner.check()
    # Darwin requires write permission on the moved directory itself. The
    # worker holds the execution lock throughout publication; jobs cannot write
    # this control-owned path through their sandbox, irrespective of file modes.
    unpacked.chmod(0o700)
    unpacked.rename(destination)
    destination.chmod(0o555)
    return receipt
