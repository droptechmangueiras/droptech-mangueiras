// Manual integrity snapshot for code/config/docs. CMS JSON and media are excluded.
import {readFile, writeFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import {resolve, dirname} from 'node:path';

const root=resolve(dirname(fileURLToPath(import.meta.url)),'..');
const files=[
  '.github/workflows/validate-site.yml','.gitattributes','.gitignore','.pages.yml',
  'GUIA-COLABORADOR.txt','LEIA-ME-ATUALIZACAO-SEGURA.txt','README.md',
  'RELATORIO-AUDITORIA.txt','SEGURANCA-E-VALIDACAO.md',
  'assets/css/style.css','assets/js/app.js',
  'catalogos.html','contato.html','galeria.html','index.html',
  'politica-privacidade.html','produto.html','produtos.html','quem-somos.html',
  'representantes.html','sobre.html',
  'docs/DEPLOY-GITHUB-PAGES.md','docs/OTIMIZACAO-IMAGENS.md',
  'package.json','package-lock.json',
  'scripts/build-site.py','scripts/checksums.mjs','scripts/image-requirements.txt',
  'scripts/media-aliases.json',
  'scripts/optimize-images.py','scripts/validate-content.mjs',
  'tests/test_image_pipeline.py','tests/site-smoke.mjs'
].sort();
const lines=['# SHA-256 of UTF-8 text normalized to LF (independent of Git autocrlf).',
  '# Refresh: node scripts/checksums.mjs --write | Verify: node scripts/checksums.mjs',
  '# Manual snapshot: CMS JSON, uploads and generated images are intentionally excluded.'];
for(const file of files){
  const text=(await readFile(resolve(root,file),'utf8')).replace(/\r\n/g,'\n');
  lines.push(`${createHash('sha256').update(text,'utf8').digest('hex')}  ./${file}`);
}
const expected=lines.join('\n')+'\n';
const target=resolve(root,'CHECKSUMS-SHA256.txt');
if(process.argv.includes('--write')){
  await writeFile(target,expected,'utf8');
  console.log(`Checksums refreshed: ${files.length} files.`);
}else{
  const actual=(await readFile(target,'utf8')).replace(/\r\n/g,'\n');
  if(actual!==expected){
    console.error('Checksums differ. Review changes, then run node scripts/checksums.mjs --write.');
    process.exitCode=1;
  }else console.log(`Checksums OK: ${files.length} files.`);
}
