import hashlib,json
from pathlib import Path
import tempfile
import unittest
from agenthub.backend_ops import verify_package_build

class BuildIntegrityTests(unittest.TestCase):
    def package(self,path):
        root=Path(path);(root/'fixture.py').write_text('value = 1\n')
        files={'fixture.py':hashlib.sha256((root/'fixture.py').read_bytes()).hexdigest()}
        ident=hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()
        (root/'BUILD_ID.json').write_text(json.dumps({'files':files,'build_id':ident}))
        return root,ident
    def test_modified_installed_file_cannot_claim_original_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root,ident=self.package(temp)
            self.assertEqual(verify_package_build(root)['build_id'],ident)
            (root/'fixture.py').write_text('value = 2\n')
            with self.assertRaisesRegex(ValueError,'runtime_file_hash_mismatch'):verify_package_build(root)
    def test_missing_installed_file_and_forged_manifest_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root,ident=self.package(temp);(root/'fixture.py').unlink()
            with self.assertRaisesRegex(ValueError,'runtime_file_hash_mismatch'):verify_package_build(root)
            root,ident=self.package(temp)
            file=root/'BUILD_ID.json';manifest=json.loads(file.read_text());manifest['build_id']='0'*64;file.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'runtime_manifest_hash_mismatch'):verify_package_build(root)
