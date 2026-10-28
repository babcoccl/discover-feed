#!/usr/bin/env node
// Collects PR history for babcoccl/discover-feed into blog/data/prs.json.
// Run manually:  GITHUB_TOKEN=... npm run history   (or rely on `gh api`)
// Never part of the site build. Applies a redaction pass and refuses to
// write output if anything suspicious survives it.

import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = 'babcoccl/discover-feed';
const OUT = join(dirname(fileURLToPath(import.meta.url)), '..', 'data', 'prs.json');

// Hostnames that must never appear in committed data (LAN addresses etc.).
// Extend via HISTORY_BLOCKLIST="host1,host2" env var.
const BLOCKED_HOSTS = (process.env.HISTORY_BLOCKLIST ?? '')
  .split(',')
  .map((s) => s.trim())
  .filter(Boolean);

const PRIVATE_IP_RE =
  /\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})\b/g;
const EMAIL_RE = /[\w.+-]+@[\w-]+\.[\w.-]+/g;
const TOKEN_RES = [
  /\bsk-[A-Za-z0-9_-]{8,}\b/g,
  /\bBearer\s+[A-Za-z0-9._~+/=-]{8,}\b/gi,
  /\b[A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD)=\S+/g,
  /\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{8,}\b/g,
];

function redact(text) {
  if (typeof text !== 'string') return text;
  let out = text
    .replace(PRIVATE_IP_RE, '[redacted-ip]')
    .replace(EMAIL_RE, '[redacted-email]');
  for (const re of TOKEN_RES) out = out.replace(re, '[redacted-secret]');
  for (const host of BLOCKED_HOSTS) {
    out = out.split(host).join('[redacted-host]');
  }
  return out;
}

function assertClean(value, path) {
  if (typeof value === 'string') {
    const bad = [];
    if (PRIVATE_IP_RE.test(value)) bad.push('private IP');
    PRIVATE_IP_RE.lastIndex = 0;
    if (TOKEN_RES.some((re) => re.test(value))) bad.push('secret-like token');
    for (const host of BLOCKED_HOSTS) if (value.includes(host)) bad.push(`blocked host ${host}`);
    if (bad.length) {
      throw new Error(`Redaction left ${bad.join(', ')} at ${path}: ${value.slice(0, 120)}`);
    }
  } else if (Array.isArray(value)) {
    value.forEach((v, i) => assertClean(v, `${path}[${i}]`));
  } else if (value && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) assertClean(v, `${path}.${k}`);
  }
}

function api(path) {
  if (process.env.GITHUB_TOKEN) {
    const res = execFileSync(
      'curl',
      [
        '-fsSL',
        '-H', `Authorization: Bearer ${process.env.GITHUB_TOKEN}`,
        '-H', 'Accept: application/vnd.github+json',
        '-H', 'X-GitHub-Api-Version: 2022-11-28',
        `https://api.github.com${path}`,
      ],
      { maxBuffer: 64 * 1024 * 1024 },
    );
    return JSON.parse(res.toString('utf8'));
  }
  const res = execFileSync('gh', ['api', path], { maxBuffer: 64 * 1024 * 1024 });
  return JSON.parse(res.toString('utf8'));
}

function apiAll(path) {
  const out = [];
  let page = 1;
  for (;;) {
    const sep = path.includes('?') ? '&' : '?';
    const chunk = api(`${path}${sep}per_page=100&page=${page}`);
    if (!Array.isArray(chunk) || chunk.length === 0) break;
    out.push(...chunk);
    if (chunk.length < 100) break;
    page += 1;
  }
  return out;
}

const prs = apiAll(`/repos/${REPO}/pulls?state=all&sort=created&direction=asc`);
console.log(`fetched ${prs.length} pull requests`);

const records = [];
for (const pr of prs) {
  const detail = api(`/repos/${REPO}/pulls/${pr.number}`);
  const commits = apiAll(`/repos/${REPO}/pulls/${pr.number}/commits`);
  records.push({
    number: pr.number,
    title: redact(pr.title ?? ''),
    state: pr.state,
    merged: Boolean(pr.merged_at ?? detail.merged_at),
    merged_at: pr.merged_at ?? detail.merged_at ?? null,
    created_at: pr.created_at,
    closed_at: pr.closed_at ?? null,
    head_branch: pr.head?.ref ?? '',
    body: redact((pr.body ?? '').slice(0, 4000)),
    additions: detail.additions ?? 0,
    deletions: detail.deletions ?? 0,
    changed_files: detail.changed_files ?? 0,
    commit_count: commits.length,
    commit_messages: commits.map((c) => redact(c.commit?.message?.split('\n')[0] ?? '')),
  });
  console.log(`#${pr.number} ${pr.state}${records.at(-1).merged ? ' (merged)' : ''}`);
}

records.sort((a, b) => a.number - b.number);
assertClean(records, 'prs');
mkdirSync(dirname(OUT), { recursive: true });
writeFileSync(OUT, JSON.stringify({ repo: REPO, generated_at: new Date().toISOString(), prs: records }, null, 2) + '\n');
console.log(`wrote ${OUT}`);
