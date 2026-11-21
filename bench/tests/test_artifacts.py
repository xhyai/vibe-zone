import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from bench.artifacts import verified_archive


class ArtifactArchiveTests(unittest.TestCase):
    def test_archive_is_verified_and_retagged_without_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            layer = b'payload for hash validation'
            config = json.dumps({'architecture':'amd64','os':'linux', 'config':{},
                                'rootfs':{'diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]}}).encode()
            with tarfile.open(root/'source.tar', 'w') as archive:
                for name, content in [('config.json', config), ('layer.tar', layer),
                    ('manifest.json',json.dumps([dict(Config='config.json',RepoTags=['trusted/tool:latest'],Layers=['layer.tar'])]).encode())]:
                    member = tarfile.TarInfo(name);member.size=len(content)
                    archive.addfile(member,io.BytesIO(content))
            self.assertEqual(verified_archive(root/'source.tar',root/'safe.tar','vibezone-job:test'),len(layer))
            with tarfile.open(root/'safe.tar') as archive:
                self.assertEqual(json.load(archive.extractfile('manifest.json'))[0]['RepoTags'], ['vibezone-job:test'])
            (root/'link.tar').symlink_to(root/'source.tar')
            with self.assertRaises(OSError):
                verified_archive(root/'link.tar',root/'new.tar','tag')
            (root/'output-link.tar').symlink_to(root/'source.tar')
            with self.assertRaises(OSError):
                verified_archive(root/'source.tar',root/'output-link.tar','tag')

    def test_link_manifest_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)
            with tarfile.open(path/'bad.tar','w') as archive:
                member=tarfile.TarInfo('manifest.json');member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd'
                archive.addfile(member)
            with self.assertRaises(ValueError):
                verified_archive(path/'bad.tar',path/'out.tar','tag')
