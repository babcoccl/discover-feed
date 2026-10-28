#!/usr/bin/env node
// Leak scan for the blog: fails on private IPs, emails, secret-like tokens,
// and on raw prs.json / PR body content appearing in the built site.
// Usage: node scripts/scan.mjs   (run after `npm run build`; also scans src/)

import { readdirSync, readFileSync, existsSync } from 'node:fs';
import { join, dirname, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const DIST = join(ROOT, 'dist');
const PRS = join(ROOT, 'data', 'prs.json');

const PRIVATE_IP_RE =
  /\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b/;
const EMAIL_RE = /[\w.+-]+@[\w-]+\.[\w.-]+/;
const EMAIL_ALLOW = [/@(?:w3\.org|example\.com|github\.com|localhost)\b/, /^$/, /@astro/];
const SECRET_RES = [
  /\bsk-[A-Za-z0-9_-]{8,}\b/,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b/i,
  /\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)=\S+/,
  /\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}\b/,
];
const TEXT_EXT = new Set(['.html', '.xml', '.txt', '.json', '.js', '.mjs', '.ts', '.css', '.astro', '.md', '.svg', '.map']);

function* walk(dir) {
  if (!existsSync(dir)) return;
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) yield* walk(p);
    else if (TEXT_EXT.has(extname(e.name).toLowerCase())) yield p;
  }
}

let failures = 0;
const fail = (msg) => { failures += 1; console.error(`FAIL ${msg}`); };

// Load PR data to derive fingerprints that must not appear in dist/.
const prs = JSON.parse(readFileSync(PRS, 'utf8')).prs;
const titlePrefixes = prs.map((p) => p.title.replace(/\s+/g, ' ').trim().slice(0, 80));
const bodyFingerprints = prs
  .flatMap((p) => [p.body, ...(p.commit_messages ?? [])])
  .filter(Boolean)
  .map((s) => s.replace(/\s+/g, ' ').trim().slice(0, 80))
  .filter((s) => s.length >= 40 && !titlePrefixes.some((t) => t.startsWith(s) || s.startsWith(t.slice(0, 40))));
const branchNames = prs.map((p) => p.head_branch).filter(Boolean);

function scanSecrets(file, text) {
  const rel = file.slice(ROOT.length + 1);
  if (PRIVATE_IP_RE.test(text)) fail(`${rel}: private IP`);
  for (const re of SECRET_RES) if (re.test(text)) fail(`${rel}: secret-like token (${re})`);
  for (const m of text.matchAll(new RegExp(EMAIL_RE, 'g'))) {
    if (!EMAIL_ALLOW.some((a) => a.test(m[0]))) fail(`${rel}: email ${m[0]}`);
  }
}

// 1. src/ + public/: no private IPs, emails, secrets.
for (const dir of [SRC, join(ROOT, 'public')]) {
  for (const f of walk(dir)) scanSecrets(f, readFileSync(f, 'utf8'));
}

// 2. dist/: same, plus no raw prs.json structure, bodies, commit messages or
//    branch names (pages may only show number/title/dates/sizes).
if (existsSync(DIST)) {
  for (const f of walk(DIST)) {
    const text = readFileSync(f, 'utf8');
    const rel = f.slice(ROOT.length + 1);
    scanSecrets(f, text);
    if (/"head_branch"\s*:|"commit_messages"\s*:|"generated_at"\s*:/.test(text)) {
      fail(`${rel}: raw prs.json structure leaked`);
    }
    for (const frag of bodyFingerprints) {
      if (text.includes(frag)) {
        fail(`${rel}: PR body/commit text leaked: "${frag.slice(0, 60)}…"`);
        break;
      }
    }
    for (const b of branchNames) {
      if (text.includes(b)) {
        fail(`${rel}: PR head branch leaked: ${b}`);
        break;
      }
    }
  }
} else {
  console.warn('warn: dist/ not found — run `npm run build` first (src/ still scanned)');
}

if (failures) {
  console.error(`\n${failures} scan failure(s)`);
  process.exit(1);
}
console.log('scan clean');
