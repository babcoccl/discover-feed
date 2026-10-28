import raw from '../../data/prs.json';

export interface Pr {
  number: number;
  title: string;
  state: string;
  merged: boolean;
  merged_at: string | null;
  created_at: string;
  closed_at: string | null;
  head_branch: string;
  additions: number;
  deletions: number;
  changed_files: number;
  commit_count: number;
}

// Bodies and commit messages stay out of the public surface entirely.
interface PrFile {
  repo: string;
  generated_at: string;
  prs: (Pr & { body: string; commit_messages: string[] })[];
}

const data = raw as unknown as PrFile;

export const allPrs: Pr[] = data.prs.map(({ body: _b, commit_messages: _c, ...rest }) => rest);

export function prByNumber(n: number): Pr | undefined {
  return allPrs.find((p) => p.number === n);
}

export function prsFor(numbers: number[]): Pr[] {
  return numbers.map((n) => prByNumber(n)).filter((p): p is Pr => Boolean(p));
}

export const repoUrl = `https://github.com/${data.repo}`;
export const prUrl = (n: number) => `${repoUrl}/pull/${n}`;

export function mergedPrs(): Pr[] {
  return allPrs.filter((p) => p.merged);
}

export function stats() {
  const merged = mergedPrs();
  const dates = allPrs.map((p) => p.created_at).concat(merged.map((p) => p.merged_at!));
  const first = new Date(Math.min(...dates.map((d) => Date.parse(d))));
  const last = new Date(Math.max(...dates.map((d) => Date.parse(d))));
  const days = Math.max(1, Math.round((last.getTime() - first.getTime()) / 86_400_000) + 1);
  return {
    total: allPrs.length,
    merged: merged.length,
    unmerged: allPrs.length - merged.length,
    days,
    additions: merged.reduce((s, p) => s + p.additions, 0),
    deletions: merged.reduce((s, p) => s + p.deletions, 0),
    first,
    last,
  };
}

export function fmtDate(iso: string | null): string {
  if (!iso) return '—';
  return new Date(iso).toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  });
}

export function prChapter(n: number): number {
  if (n <= 7) return 1;
  if (n <= 14) return 2;
  if (n <= 17) return 3;
  if (n === 24 || (n >= 27 && n <= 34)) return 5;
  if (n <= 26) return 4;
  return 6;
}

export const CHAPTERS = [
  { n: 1, title: 'Scaffolding, ingestion and clustering', dates: 'Oct 7–8' },
  { n: 2, title: 'Summaries, briefs, reports and the API', dates: 'Oct 8–12' },
  { n: 3, title: 'Alerts, sources and card images', dates: 'Oct 12–14' },
  { n: 4, title: 'Web-search enrichment and hardening', dates: 'Oct 15–17' },
  { n: 5, title: 'Context links, deep coverage and perspectives', dates: 'Oct 17–20' },
  { n: 6, title: 'Observability and the SQLite lock hunt', dates: 'Oct 21–25' },
] as const;
