import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path

from macqueue.common import Invalid, digest
from macqueue.sources import lock_identity, package, unpack


class SourcePackageTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def test_links_traversal_duplicates_and_unexpected_files_are_rejected(self):
        for index, (name, kind) in enumerate((('../escape', tarfile.REGTYPE), ('/absolute', tarfile.REGTYPE),
                    ('vendor/link', tarfile.SYMTYPE), ('vendor/hard', tarfile.LNKTYPE),
                    ('vendor/fifo', tarfile.FIFOTYPE), ('worker.token', tarfile.REGTYPE),
                    ('vendor/../escape', tarfile.REGTYPE), ('vendor/duplicate', tarfile.REGTYPE))):
            archive = self.root / f'{index}.tar.gz'
            with tarfile.open(archive, 'w:gz') as tar:
                info = tarfile.TarInfo(name)
                info.type, info.linkname = kind, '/etc/passwd'
                tar.addfile(info, io.BytesIO(b''))
                if name == 'vendor/duplicate':
                    tar.addfile(info, io.BytesIO(b''))
            with self.subTest(name=name), self.assertRaises((Invalid, ValueError)):
                unpack(archive, self.root / f'extracted-{index}')
        self.assertFalse((self.root / 'escape').exists())

    def test_package_identity_is_deterministic_and_manifest_is_preserved(self):
        bundle = self.root / 'source.bundle'
        bundle.write_bytes(b'Git bundle placeholder')
        vendor = self.root / 'vendor'
        vendor.mkdir()
        (vendor / 'file').write_text('dependency bytes')
        manifest = {'version': 1, 'project': 'seedfinder', 'revisions': {'a'*40: 'b'*64}, 'bundle_sha256': digest(bundle)}
        for name in ('first', 'second'):
            package(bundle, vendor, manifest, self.root / (name + '.tar.gz'))
        self.assertEqual(digest(self.root / 'first.tar.gz'), digest(self.root / 'second.tar.gz'))
        extracted = self.root / 'extracted'
        self.assertEqual(manifest, unpack(self.root / 'first.tar.gz', extracted))
        self.assertEqual('dependency bytes', (extracted / 'vendor/file').read_text())

    def test_lockfile_symlinks_and_custom_sources_are_rejected(self):
        checkout = self.root / 'checkout'
        checkout.mkdir()
        secret = self.root / 'secret'
        secret.write_text('private')
        (checkout / 'Cargo.lock').symlink_to(secret)
        with self.assertRaisesRegex(Invalid, 'symlink'):
            lock_identity(checkout)
        (checkout / 'Cargo.lock').unlink()
        (checkout / 'Cargo.lock').write_text('version = 4\n[[package]]\nname="test"\nsource="git+https://example.invalid/repo"\n')
        with self.assertRaisesRegex(Invalid, 'other sources need operator review'):
            lock_identity(checkout)

    def test_oversized_extension_header_is_rejected_before_reading_its_body(self):
        archive = self.root / 'header.tar.gz'
        import gzip
        with gzip.open(archive, 'wb') as stream:
            info = tarfile.TarInfo('pax')
            info.type, info.size = tarfile.XHDTYPE, 100000000
            stream.write(info.tobuf())
        with self.assertRaisesRegex(Invalid, 'extension header too large'):
            unpack(archive, self.root / 'header-output')
