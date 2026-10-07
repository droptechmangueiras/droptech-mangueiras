"""Generate, validate and package the static site; never publish or commit it."""
from pathlib import Path
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from urllib.parse import unquote, urlsplit

from importlib.util import module_from_spec, spec_from_file_location

sys.dont_write_bytecode = True


def image_generator(root):
    spec = spec_from_file_location('optimize_images', root / 'scripts/optimize-images.py')
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_files(root):
    files = list(root.glob('*.html'))
    for directory in ['assets', 'conteudo', 'favicon']:
        files.extend(p for p in (root / directory).rglob('*') if p.is_file())
    for name in ['.nojekyll', 'CNAME', 'robots.txt', 'sitemap.xml']:
        if (root / name).is_file():
            files.append(root / name)
    return sorted(files)


def add_media_aliases(root, output):
    """Restore legacy PDF URLs in the artifact without duplicating Git uploads."""
    config = root / 'scripts/media-aliases.json'
    if not config.is_file():
        return
    aliases = json.loads(config.read_text(encoding='utf-8'))
    if not isinstance(aliases, dict):
        raise ValueError('Media aliases must be an object')

    def pdf_path(ref):
        if not isinstance(ref, str) or not ref.startswith('assets/uploads/') or '\\' in ref:
            raise ValueError(f'Invalid PDF alias path: {ref!r}')
        if any(part in ('', '.', '..') for part in ref.split('/')) or Path(ref).suffix.lower() != '.pdf':
            raise ValueError(f'Invalid PDF alias path: {ref!r}')
        result = output / ref
        if not result.resolve().is_relative_to(output.resolve()):
            raise ValueError(f'PDF alias escapes artifact: {ref!r}')
        return result

    for alias, original in aliases.items():
        target, source = pdf_path(alias), pdf_path(original)
        # Never overwrite a real upload that occupies a legacy URL.
        if target.is_file():
            continue
        if not source.is_file():
            raise ValueError(f'Missing PDF alias source: {original}')
        with source.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise ValueError(f'Invalid PDF alias source: {original}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        print(f'PDF compatibility URL: {alias} -> {original}', flush=True)


def validate_artifact(output):
    """Check static URLs, including srcset, and CMS media inside the actual artifact."""
    if not (output / 'index.html').is_file():
        raise ValueError('Artifact is missing index.html')

    def check(ref, origin):
        ref = ref.strip()
        if not ref or ref.startswith('#'):
            return
        url = urlsplit(ref)
        if url.scheme or url.netloc:
            return
        candidate = output / unquote(url.path).lstrip('/') if url.path.startswith('/') else origin.parent / unquote(url.path)
        resolved = candidate.resolve()
        if not resolved.is_relative_to(output) or not resolved.is_file():
            raise ValueError(f'Missing artifact reference in {origin.relative_to(output)}: {ref}')

    for source in output.glob('*.html'):
        text = source.read_text(encoding='utf-8')
        for ref in re.findall(r'''\b(?:src|href|action)=["']([^"']+)["']''', text):
            check(ref, source)
        for srcset in re.findall(r'''\bsrcset=["']([^"']+)["']''', text):
            for item in srcset.split(','):
                check(item.strip().split()[0], source)
    for source in (output / 'assets/css').glob('*.css'):
        for ref in re.findall(r'''url\(\s*["']?([^\)"']+)["']?\s*\)''', source.read_text(encoding='utf-8')):
            check(ref, source)

    def media_refs(value):
        if isinstance(value, dict):
            for child in value.values():
                yield from media_refs(child)
        elif isinstance(value, list):
            for child in value:
                yield from media_refs(child)
        elif isinstance(value, str) and value.lstrip('/').startswith(('assets/', 'favicon/')):
            yield value

    for source in (output / 'conteudo').glob('*.json'):
        for ref in media_refs(json.loads(source.read_text(encoding='utf-8'))):
            check('/' + ref.lstrip('/'), source)
    manifest = json.loads((output / 'assets/optimized/manifest.json').read_text(encoding='utf-8'))
    for original, info in manifest.items():
        check('/' + original, output / 'index.html')
        for variant in info['variants']:
            check('/' + variant['path'], output / 'index.html')


def build(root, output, node='node', cache=None):
    root, output = root.resolve(), output.resolve()
    if output == root or output.is_relative_to(root):
        raise ValueError('Artifact directory must be outside the source checkout')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Artifact directory must be empty; existing files are not removed')
    generator = image_generator(root)
    print(json.dumps(generator.generate(root, cache), indent=2), flush=True)
    for source in sorted((root / 'assets/js').glob('*.js')) + sorted((root / 'scripts').glob('*.mjs')):
        subprocess.run([node, '--check', str(source)], cwd=root, check=True)
    subprocess.run([node, 'scripts/validate-content.mjs', '--strict-optimized'], cwd=root, check=True)
    generator.generate(root, check=True)
    # Packaging is reached only after generation and every validation succeeded.
    files = public_files(root)
    for source in files:
        if source.is_symlink() or not source.resolve().is_relative_to(root):
            raise ValueError(f'Links are not allowed in the Pages artifact: {source}')
    output.mkdir(parents=True, exist_ok=True)
    for source in files:
        target = output / source.relative_to(root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    add_media_aliases(root, output)
    validate_artifact(output)
    count = sum(1 for p in output.rglob('*') if p.is_file())
    print(f'Static artifact validated: {count} files in {output}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--node', default=os.environ.get('NODE_BINARY', 'node'))
    args = parser.parse_args()
    build(args.root, args.output, args.node, args.cache_dir)


if __name__ == '__main__':
    main()
