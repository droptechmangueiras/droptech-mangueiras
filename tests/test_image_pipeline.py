"""End-to-end image pipeline regressions; fixtures never touch repository media."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
NODE = os.environ.get('NODE_BINARY', 'node')

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

OPT = load('images_test', REPO / 'scripts/optimize-images.py')
BUILD = load('build_test', REPO / 'scripts/build-site.py')

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='droptech-pipeline-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'site'
        for directory in ['assets/uploads', 'assets/js', 'assets/css', 'conteudo', 'scripts']:
            (self.root / directory).mkdir(parents=True)
        for script in ['optimize-images.py', 'validate-content.mjs']:
            shutil.copyfile(REPO / 'scripts' / script, self.root / 'scripts' / script)
        for name in ['produtos', 'categorias', 'galeria', 'clientes', 'catalogos', 'certificacoes']:
            self.json(name, [])
        self.json('empresa', {})
        (self.root / 'assets/js/app.js').write_text('"use strict";', encoding='utf-8')
        (self.root / 'assets/css/style.css').write_text('img { max-width:100%; }', encoding='utf-8')
        (self.root / '.nojekyll').touch()
        (self.root / '.pages.yml').write_text('media: assets/uploads\n', encoding='utf-8')
        self.photo('photo.jpg', 1)
        self.html = '<html><head><meta http-equiv="Content-Security-Policy" content="default-src self"><meta name="referrer" content="strict-origin"></head><body><img src="assets/uploads/photo.jpg"></body></html>'
        (self.root / 'index.html').write_text(self.html, encoding='utf-8')

    def json(self, name, value):
        (self.root / 'conteudo' / (name + '.json')).write_text(json.dumps(value), encoding='utf-8')

    def photo(self, name, seed):
        # Deterministic textured photo: useful compression at multiple dimensions.
        image = Image.new('RGB', (1100, 1050))
        image.putdata([((x * 7 + seed * 41) % 256, (y * 3 + seed) % 256, (x + y) % 256)
                       for y in range(1050) for x in range(1100)])
        image.save(self.root / 'assets/uploads' / name, quality=95)

    def manifest(self):
        return json.loads((self.root / 'assets/optimized/manifest.json').read_text(encoding='utf-8'))

    def derivatives(self):
        return {p.name: digest(p) for p in (self.root / 'assets/optimized').glob('*.webp')}

    def test_unchanged_and_content_only_do_not_encode(self):
        original = digest(self.root / 'assets/uploads/photo.jpg')
        first = OPT.generate(self.root)
        self.assertGreater(first['encoded'], 0)
        before = (self.root / 'assets/optimized/manifest.json').read_bytes()
        files = self.derivatives()
        self.json('empresa', {'nome': 'New text'})
        with patch.object(Image.Image, 'save', side_effect=AssertionError('unnecessary encoding')):
            self.assertEqual(OPT.generate(self.root)['encoded'], 0)
            self.assertEqual(OPT.generate(self.root, check=True)['encoded'], 0)
        self.assertEqual(before, (self.root / 'assets/optimized/manifest.json').read_bytes())
        self.assertEqual(files, self.derivatives())
        self.assertEqual(original, digest(self.root / 'assets/uploads/photo.jpg'))

    def test_upload_first_json_later_and_new_reference(self):
        OPT.generate(self.root)
        self.photo('new.jpg', 2)
        self.assertEqual(OPT.generate(self.root)['encoded'], 0)
        self.assertNotIn('assets/uploads/new.jpg', self.manifest())
        self.json('clientes', [{'logo': 'assets/uploads/new.jpg'}])
        result = OPT.generate(self.root)
        self.assertEqual(result['reused'], 1)
        self.assertGreater(result['encoded'], 0)
        self.assertIn('assets/uploads/new.jpg', self.manifest())
        OPT.generate(self.root, check=True)

    def test_same_path_new_bytes_retains_old_derivatives(self):
        OPT.generate(self.root)
        previous = self.derivatives()
        old_sha = self.manifest()['assets/uploads/photo.jpg']['sha256']
        self.photo('photo.jpg', 3)
        with self.assertRaises(ValueError):
            OPT.generate(self.root, check=True)
        self.assertGreater(OPT.generate(self.root)['encoded'], 0)
        self.assertNotEqual(old_sha, self.manifest()['assets/uploads/photo.jpg']['sha256'])
        self.assertEqual(previous, {name: self.derivatives()[name] for name in previous})
        OPT.generate(self.root, check=True)

    def test_cache_restores_without_encoding(self):
        clean = self.base / 'clean'
        shutil.copytree(self.root, clean)
        cache = self.base / 'cache'
        OPT.generate(self.root, cache)
        with patch.object(Image.Image, 'save', side_effect=AssertionError('cache should reuse')):
            result = OPT.generate(clean, cache)
        self.assertEqual(result['restored'], 1)
        self.assertEqual(result['encoded'], 0)
        self.assertEqual((self.root / 'assets/optimized/manifest.json').read_bytes(),
                         (clean / 'assets/optimized/manifest.json').read_bytes())

    def test_parameter_change_creates_new_variants(self):
        OPT.generate(self.root)
        previous = self.derivatives()
        original_parameters = OPT.parameters
        with patch.object(OPT, 'parameters', side_effect=lambda ref, alpha: dict(original_parameters(ref, alpha), quality=90)):
            self.assertGreater(OPT.generate(self.root)['encoded'], 0)
            self.assertEqual(OPT.generate(self.root)['encoded'], 0)
        self.assertEqual(previous, {name: self.derivatives()[name] for name in previous})

    def test_corrupt_derivative_is_detected_without_overwriting(self):
        OPT.generate(self.root)
        variant = self.root / self.manifest()['assets/uploads/photo.jpg']['variants'][0]['path']
        variant.write_bytes(b'broken temporary fixture')
        with self.assertRaises(ValueError):
            OPT.generate(self.root, check=True)
        with self.assertRaises(ValueError):
            OPT.generate(self.root)
        self.assertEqual(variant.read_bytes(), b'broken temporary fixture')

    def test_invalid_content_blocks_packaging(self):
        (self.root / 'index.html').write_text(self.html.replace('<body>', '<body><div id="duplicate"></div><div id="duplicate"></div>'), encoding='utf-8')
        output = self.base / 'invalid-artifact'
        run = subprocess.run
        # Capture the real child process: redirect_stdout only captures Python.
        # Expected ::error:: output must not become an Actions error annotation.
        def capture_run(*args, **kwargs):
            return run(*args, **kwargs, capture_output=True, text=True, encoding='utf-8')
        with contextlib.redirect_stdout(io.StringIO()), patch.object(BUILD.subprocess, 'run', side_effect=capture_run), self.assertRaises(subprocess.CalledProcessError) as failure:
            BUILD.build(self.root, output, NODE)
        self.assertEqual(failure.exception.returncode, 1)
        self.assertIn('IDs HTML duplicados: duplicate', failure.exception.stderr)
        self.assertFalse(output.exists())

    def test_pdf_aliases_are_packaged_without_changing_uploads(self):
        original = self.root / 'assets/uploads/catalog (1).pdf'
        original.write_bytes(b'%PDF-1.7\nfixture')
        aliases = {'assets/uploads/legacy.pdf': 'assets/uploads/catalog (1).pdf',
                   'assets/uploads/catalogos/legacy.pdf': 'assets/uploads/catalog (1).pdf'}
        (self.root / 'scripts/media-aliases.json').write_text(json.dumps(aliases), encoding='utf-8')
        output = self.base / 'pdf-artifact'
        with contextlib.redirect_stdout(io.StringIO()):
            BUILD.build(self.root, output, NODE)
        for alias in aliases:
            self.assertEqual((output / alias).read_bytes(), original.read_bytes())
            self.assertFalse((self.root / alias).exists())
        self.assertFalse((output / 'scripts/media-aliases.json').exists())

    def test_pdf_alias_preserves_existing_upload(self):
        config = self.root / 'scripts/media-aliases.json'
        config.write_text(json.dumps({'assets/uploads/legacy.pdf': 'assets/uploads/absent.pdf'}), encoding='utf-8')
        original = self.root / 'assets/uploads/legacy.pdf'
        original.write_bytes(b'%PDF-1.7\noriginal upload')
        BUILD.add_media_aliases(self.root, self.root)
        self.assertEqual(original.read_bytes(), b'%PDF-1.7\noriginal upload')

    def test_pdf_alias_rejects_missing_invalid_and_unsafe_sources(self):
        source = self.root / 'assets/uploads/invalid.pdf'
        source.write_bytes(b'not a PDF')
        cases = [[], {'assets/uploads/legacy.pdf': 'assets/uploads/missing.pdf'},
                 {'assets/uploads/legacy.pdf': 'assets/uploads/invalid.pdf'},
                 {'assets/uploads/../../../escape.pdf': 'assets/uploads/invalid.pdf'},
                 {'assets/uploads/legacy.pdf': '../outside.pdf'}]
        for aliases in cases:
            with self.subTest(aliases=aliases):
                (self.root / 'scripts/media-aliases.json').write_text(json.dumps(aliases), encoding='utf-8')
                with self.assertRaises(ValueError):
                    BUILD.add_media_aliases(self.root, self.root)
        self.assertFalse((self.root / 'assets/uploads/legacy.pdf').exists())

    def test_missing_media_blocks_packaging(self):
        self.json('clientes', [{'logo': 'assets/uploads/missing.jpg'}])
        output = self.base / 'missing-artifact'
        with self.assertRaises(ValueError):
            BUILD.build(self.root, output, NODE)
        self.assertFalse(output.exists())

    def test_artifact_is_deterministic_and_preserves_fallback(self):
        outputs = [self.base / 'artifact-one', self.base / 'artifact-two']
        for output in outputs:
            with contextlib.redirect_stdout(io.StringIO()):
                BUILD.build(self.root, output, NODE)
        snapshots = [{str(p.relative_to(output)): digest(p) for p in output.rglob('*') if p.is_file()}
                     for output in outputs]
        self.assertEqual(*snapshots)
        self.assertEqual((outputs[0] / 'index.html').read_text(encoding='utf-8'), self.html)
        self.assertEqual(digest(outputs[0] / 'assets/uploads/photo.jpg'), digest(self.root / 'assets/uploads/photo.jpg'))
        self.assertTrue((outputs[0] / '.nojekyll').is_file())
        self.assertFalse((outputs[0] / 'scripts').exists())
        self.assertFalse((outputs[0] / '.git').exists())

    def test_missing_srcset_is_rejected(self):
        OPT.generate(self.root)
        output = self.base / 'bad-srcset'
        shutil.copytree(self.root, output)
        (output / 'index.html').write_text(self.html.replace('<img ', '<img srcset="assets/optimized/missing.webp 400w" '), encoding='utf-8')
        with self.assertRaises(ValueError):
            BUILD.validate_artifact(output)

if __name__ == '__main__':
    unittest.main()
