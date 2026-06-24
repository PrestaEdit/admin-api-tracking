#!/usr/bin/env python3
"""
Self-contained generator for the PrestaShop Admin API endpoint tracking page.

Pipeline (no manual steps):
  1. Fetch issue #39630 body from PrestaShop/PrestaShop via `gh`.
  2. Parse the per-domain markdown tables into structured rows.
  3. Collect every referenced ps_apiresources PR and query its live state via `gh`.
  4. Reconcile statuses:  PR merged -> Implemented,  PR closed (not merged) -> Missing,
     PR still open -> In Progress.  The verified PR author is attached to each row.
  4b. Verify real coverage against the dev code: any 'Missing' row whose CQRS class is
     already present in ps_apiresources@dev (merged under a non-obvious path the stale
     issue never updated) is flipped to Implemented, with author credited via blame.
  5. Render a standalone, interactive HTML file styled with Preline UI / Tailwind
     (dark mode, search / filters by status, type, domain, author / sorts).

Requirements at runtime: python3 + an authenticated `gh` CLI.
Usage: python3 admin-api-tracking.gen.py [output.html]
"""
import json, re, subprocess, sys, os, io, tarfile
from datetime import datetime, timezone

REPO_CORE = "PrestaShop/PrestaShop"
REPO_API = "PrestaShop/ps_apiresources"
ISSUE = "39630"
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "admin-api-tracking.html")

STATUS_MAP = {'✅ Implemented': 'implemented', '🚧 In Progress': 'in_progress', '❌ Missing': 'missing'}
PR_RE = re.compile(r'\[([^\]]+)\]\(https://github.com/([^)]+)\).*?/ps_apiresources/pull/(\d+)')


def gh_json(args):
    out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError("gh failed: " + " ".join(args) + "\n" + out.stderr)
    return json.loads(out.stdout)


def fetch_issue_body():
    return gh_json(["issue", "view", ISSUE, "--repo", REPO_CORE, "--json", "body"])["body"]


def parse(body):
    domains, cur, seen = [], None, set()
    for ln in body.splitlines():
        m = re.match(r'## .*Domain: (.+)', ln)
        if m:
            cur = {'name': m.group(1).strip(), 'rows': []}
            domains.append(cur)
            continue
        if ln.startswith('|') and cur is not None:
            cells = [c.strip() for c in ln.strip().strip('|').split('|')]
            if len(cells) < 5:
                continue
            if cells[0] == 'Action' or set(cells[0]) <= set('-:'):
                continue
            action = cells[0].strip('`')
            typ = cells[1]
            status = STATUS_MAP.get(cells[2], 'missing')
            endpoint = cells[3]
            assignee = pr = None
            am = PR_RE.search(cells[4])
            if am:
                assignee, pr = am.group(1), int(am.group(3))
            key = (cur['name'], action, typ, endpoint)
            if key in seen:           # drop exact-duplicate generator artifacts
                continue
            seen.add(key)
            cur['rows'].append({'action': action, 'type': typ, 'status': status,
                                'endpoint': endpoint, 'assignee': assignee, 'pr': pr,
                                'author': None})
    return domains


def pr_info(num):
    """Return (state, author_login). state normalized to OPEN/MERGED/CLOSED.
    Uses the REST pulls endpoint (reliable for public cross-repo reads with any token,
    including the Actions GITHUB_TOKEN) — avoids GraphQL, which 401s intermittently."""
    try:
        d = gh_json(["api", f"repos/{REPO_API}/pulls/{num}"])
        if d.get("merged"):
            state = "MERGED"
        elif d.get("state") == "closed":
            state = "CLOSED"
        else:
            state = "OPEN"
        return state, (d.get("user") or {}).get("login")
    except Exception as e:
        sys.stderr.write(f"warn: PR #{num}: {e}\n")
        return None, None


def reconcile(domains):
    prs = sorted({r['pr'] for d in domains for r in d['rows'] if r['pr']})
    info = {n: pr_info(n) for n in prs}
    moved_merged, moved_closed = [], []
    for d in domains:
        for r in d['rows']:
            if not r['pr'] or r['status'] != 'in_progress':
                continue
            st, author = info.get(r['pr'], (None, None))
            if author:
                r['assignee'] = author          # verified real PR creator (overrides issue listing)
                r['author'] = author            # preserved for the author filter even after a transition
            if st == 'MERGED':
                r['status'] = 'implemented'
                r['merged_pr'] = r['pr']
                # keep assignee/author to credit the contributor
                if not r['endpoint']:
                    r['endpoint'] = ''          # endpoint detail not auto-derived; PR link kept
                moved_merged.append((d['name'], r['action'], r['pr']))
            elif st == 'CLOSED':
                r['status'] = 'missing'
                r['endpoint'] = ''
                r['assignee'] = r['pr'] = r['author'] = None
                moved_closed.append((d['name'], r['action']))
    return info, moved_merged, moved_closed


def discover_unlisted_prs(domains, referenced):
    """Catch OPEN ps_apiresources PRs the (periodically-regenerated, often-stale)
    issue table hasn't captured yet: link each to the Missing rows whose CQRS command
    class name appears in the PR diff, flipping them to In Progress with the real author."""
    try:
        open_prs = gh_json(["pr", "list", "--repo", REPO_API, "--state", "open",
                            "--limit", "200", "--json", "number,author"])
    except Exception as e:
        sys.stderr.write(f"warn: pr list: {e}\n")
        return []
    missing_by_action = {}
    for d in domains:
        for r in d['rows']:
            if r['status'] == 'missing':
                missing_by_action.setdefault(r['action'], []).append(r)
    linked = []
    for pr in open_prs:
        num = pr['number']
        if num in referenced:
            continue
        author = (pr.get('author') or {}).get('login')
        try:
            diff = subprocess.run(["gh", "pr", "diff", str(num), "--repo", REPO_API],
                                  capture_output=True, text=True, timeout=60).stdout
        except Exception as e:
            sys.stderr.write(f"warn: pr diff {num}: {e}\n")
            continue
        for action, rows in missing_by_action.items():
            if re.search(r'\b' + re.escape(action) + r'\b', diff):
                for r in rows:
                    if r['status'] == 'missing':       # first PR to claim the row wins
                        r['status'] = 'in_progress'
                        r['pr'] = num
                        r['author'] = r['assignee'] = author
                        linked.append((num, action, author))
    return linked


RES_DIR = "src/ApiPlatform/Resources/"


def fetch_dev_resources():
    """Download the ps_apiresources@dev tarball once (single REST call, works with the
    Actions GITHUB_TOKEN on this public repo) and return {relative_path: file_content}
    for every PHP file under src/ApiPlatform/Resources/. Returns {} on any failure so the
    daily job never breaks on this best-effort step."""
    try:
        out = subprocess.run(["gh", "api", f"repos/{REPO_API}/tarball/dev"],
                             capture_output=True, timeout=120)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode("utf-8", "replace"))
        files = {}
        with tarfile.open(fileobj=io.BytesIO(out.stdout), mode="r:gz") as tar:
            for m in tar.getmembers():
                if not (m.isfile() and m.name.endswith(".php")):
                    continue
                # strip the tarball's top-level "<owner>-<repo>-<sha>/" prefix
                rel = m.name.split("/", 1)[1] if "/" in m.name else m.name
                if RES_DIR not in rel:
                    continue
                rel = rel[rel.index(RES_DIR):]
                f = tar.extractfile(m)
                if f is not None:
                    files[rel] = f.read().decode("utf-8", "replace")
        return files
    except Exception as e:
        sys.stderr.write(f"warn: fetch dev resources: {e}\n")
        return {}


def blame_author(path):
    """Login of the most recent committer to touch a dev file (REST, token-safe)."""
    try:
        d = gh_json(["api", f"repos/{REPO_API}/commits?path={path}&sha=dev&per_page=1"])
        if d:
            return ((d[0].get("author") or {}).get("login")
                    or (d[0].get("commit", {}).get("author") or {}).get("name"))
    except Exception as e:
        sys.stderr.write(f"warn: blame {path}: {e}\n")
    return None


def discover_implemented_in_code(domains):
    """Catch FALSE 'Missing' rows: endpoints already merged into ps_apiresources@dev but
    under non-obvious paths/names the stale issue never updated (lesson from PR #241 —
    AttributeGroup/CustomerGroup were already done). For each Missing row, if its CQRS
    class name appears in a dev Resource file, flip it to Implemented, link the file, and
    credit the file's latest committer via blame."""
    files = fetch_dev_resources()
    if not files:
        return []
    rescued = []
    for d in domains:
        for r in d['rows']:
            if r['status'] != 'missing':
                continue
            pat = re.compile(r'\b' + re.escape(r['action']) + r'\b')
            hit = next((p for p, c in files.items() if pat.search(c)), None)
            if not hit:
                continue
            r['status'] = 'implemented'
            r['in_code'] = True
            r['src'] = hit
            r['author'] = r['assignee'] = blame_author(hit)
            rescued.append((d['name'], r['action'], hit))
    return rescued


# Earliest PS version whose core CQRS class exists, by label (oldest first).
# 9.0 = buildable NOW (the ps_apiresources CI matrix floor is 9.0.3);
# 9.1 = class introduced in 9.1, only buildable once 9.0.x is dropped from the matrix;
# dev = class only on develop (future).
CORE_REFS = [("9.0", "9.0.3"), ("9.1", "9.1.x"), ("dev", "develop")]


def _core_domain_tree_sha(ref):
    """SHA of the src/Core/Domain tree at a ref (one cheap REST call, token-safe)."""
    try:
        d = gh_json(["api", f"repos/{REPO_CORE}/contents/src/Core?ref={ref}"])
    except Exception as e:
        sys.stderr.write(f"warn: core tree {ref}: {e}\n")
        return None
    return next((e["sha"] for e in d
                 if e.get("name") == "Domain" and e.get("type") == "dir"), None)


def _core_class_names(ref):
    """Set of every Command/Query class short-name under src/Core/Domain at a ref.
    Fetches only the Domain subtree (~2.6k entries, never truncated), not the whole repo."""
    sha = _core_domain_tree_sha(ref)
    if not sha:
        return set()
    try:
        d = gh_json(["api", f"repos/{REPO_CORE}/git/trees/{sha}?recursive=1"])
    except Exception as e:
        sys.stderr.write(f"warn: core subtree {ref}: {e}\n")
        return set()
    names = set()
    for t in d.get("tree", []):
        m = re.search(r'(?:^|/)(?:Command|Query)/([A-Za-z]+)\.php$', t.get("path", ""))
        if m:
            names.add(m.group(1))
    return names


def annotate_versions(domains):
    """Tag every row with the EARLIEST PS version whose core CQRS class exists, so the page
    shows what is implementable under which version constraint. Best-effort: on any fetch
    failure the version stays None (rendered as 'n/a') and the rest of the page is unaffected.
    Returns the {className: label} index for logging."""
    idx = {}
    for label, ref in reversed(CORE_REFS):   # newest->oldest so the oldest (min) label wins
        for n in _core_class_names(ref):
            idx[n] = label
    for d in domains:
        for r in d['rows']:
            r['version'] = idx.get(r['action'])
    return idx


def build(domains, generated_at, merged_note):
    domains.sort(key=lambda d: d['name'].lower())
    total = sum(len(d['rows']) for d in domains)
    impl = sum(1 for d in domains for r in d['rows'] if r['status'] == 'implemented')
    prog = sum(1 for d in domains for r in d['rows'] if r['status'] == 'in_progress')
    miss = sum(1 for d in domains for r in d['rows'] if r['status'] == 'missing')
    data = {'domains': domains, 'total': total, 'impl': impl, 'prog': prog, 'miss': miss}
    payload = json.dumps(data, ensure_ascii=False)
    html = TEMPLATE.replace('__DATA__', payload) \
                   .replace('__DATE__', generated_at) \
                   .replace('__MERGEDNOTE__', merged_note)
    return html, (total, impl, prog, miss)


TEMPLATE = r'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PrestaShop Admin API — Endpoint Tracking</title>
<script src="https://cdn.tailwindcss.com"></script>
<script>tailwind.config={darkMode:'class'}</script>
<script>(function(){try{var t=localStorage.getItem('theme');if(t==='dark'||(!t&&window.matchMedia('(prefers-color-scheme: dark)').matches))document.documentElement.classList.add('dark');}catch(e){}})();</script>
<style>
  /* behaviour-only rules kept in CSS so the proven JS hooks stay intact */
  .hide{display:none!important}
  tr.row[hidden]{display:none}
  .domain.col .dbody{display:none}
  .domain.col .caret{transform:rotate(-90deg)}
  .caret{transition:transform .15s ease}
  .seg-btn{transition:background-color .12s ease,color .12s ease,border-color .12s ease}
  .seg-btn.on{background-color:#4f46e5;color:#fff;border-color:#4f46e5;z-index:1}
  /* colour-coded active states, coherent with the status & type badges */
  #f-status .seg-btn.on[data-v="implemented"]{background-color:#0d9488;border-color:#0d9488}
  #f-status .seg-btn.on[data-v="in_progress"]{background-color:#d97706;border-color:#d97706}
  #f-status .seg-btn.on[data-v="missing"]{background-color:#dc2626;border-color:#dc2626}
  #f-type .seg-btn.on[data-v="Command"]{background-color:#2563eb;border-color:#2563eb}
  #f-type .seg-btn.on[data-v="Query"]{background-color:#7c3aed;border-color:#7c3aed}
  #f-version .seg-btn.on[data-v="9.0"]{background-color:#059669;border-color:#059669}
  #f-version .seg-btn.on[data-v="9.1"]{background-color:#d97706;border-color:#d97706}
  #f-version .seg-btn.on[data-v="dev"]{background-color:#7c3aed;border-color:#7c3aed}
  #f-version .seg-btn.on[data-v="na"]{background-color:#6b7280;border-color:#6b7280}
  .sortable .arr{font-size:9px;opacity:.6}
  [data-c]{cursor:pointer;user-select:none}
  .tab-btn{border-bottom:2px solid transparent;color:#6b7280;margin-bottom:-1px;cursor:pointer}
  .tab-btn:hover{color:#374151}
  .tab-btn.tab-on{border-bottom-color:#4f46e5;color:#4f46e5;font-weight:600}
  .dark .tab-btn{color:#9ca3af}
  .dark .tab-btn:hover{color:#e5e7eb}
  .dark .tab-btn.tab-on{color:#818cf8;border-bottom-color:#818cf8}
</style>
</head>
<body class="bg-gray-50 dark:bg-gray-900 text-gray-800 dark:text-gray-200 antialiased">

<header class="bg-white dark:bg-gray-800 border-b border-gray-200 dark:border-gray-700">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6 flex items-start gap-4">
    <div class="grow">
      <h1 class="text-2xl font-bold text-gray-900 dark:text-white">PrestaShop Admin API — Endpoint Tracking</h1>
      <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
        CQRS Commands &amp; Queries mapped to Admin API endpoints ·
        source <a class="text-blue-600 dark:text-blue-400 hover:underline" href="https://github.com/PrestaShop/PrestaShop/issues/39630" target="_blank">issue #39630</a> ·
        endpoints live in <a class="text-blue-600 dark:text-blue-400 hover:underline" href="https://github.com/PrestaShop/ps_apiresources" target="_blank">ps_apiresources</a>
      </p>
    </div>
    <button id="theme" title="Toggle dark mode" class="shrink-0 size-10 inline-flex items-center justify-center rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-lg hover:bg-gray-50 dark:hover:bg-gray-700">🌙</button>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 sm:px-6 pb-20">

  <!-- stat cards: endpoints (always visible, outside the tabs) -->
  <h2 class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mt-6 mb-2">Endpoints</h2>
  <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-total" class="text-2xl font-bold text-gray-900 dark:text-white">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Total</div></div>
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-impl" class="text-2xl font-bold text-teal-600 dark:text-teal-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Implemented</div></div>
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-prog" class="text-2xl font-bold text-amber-500 dark:text-amber-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">In progress</div></div>
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-miss" class="text-2xl font-bold text-red-600 dark:text-red-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Missing</div></div>
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-pct" class="text-2xl font-bold text-indigo-600 dark:text-indigo-400">0%</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Progress</div></div>
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="c-proj" class="text-2xl font-bold text-indigo-400 dark:text-indigo-300">0%</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Projected</div></div>
  </div>

  <!-- overall progress -->
  <div class="mt-4">
    <div class="flex h-3 rounded-full bg-red-500 overflow-hidden">
      <div id="ov-i" class="bg-teal-500 h-full"></div>
      <div id="ov-p" class="bg-amber-400 h-full"></div>
    </div>
    <div class="flex flex-wrap gap-x-5 gap-y-1 mt-2 text-xs text-gray-500 dark:text-gray-400">
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-teal-500"></span> Implemented</span>
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-amber-400"></span> In progress (open PR)</span>
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-red-500"></span> Missing</span>
    </div>
  </div>

  <!-- info note (Preline soft alert) -->
  <div class="mt-4 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-900 border-s-4 border-s-blue-500 text-blue-800 dark:text-blue-200 rounded-lg p-4 text-sm">
    <b>Auto-generated __DATE__.</b> __MERGEDNOTE__
  </div>

  <!-- tabs -->
  <div class="mt-6 flex gap-1 border-b border-gray-200 dark:border-gray-700">
    <button data-tab="endpoints" class="tab-btn px-4 py-2.5 text-sm">Endpoints</button>
    <button data-tab="stats" class="tab-btn px-4 py-2.5 text-sm">Statistiques</button>
  </div>

  <!-- panel: endpoints -->
  <div data-panel="endpoints">

  <!-- controls -->
  <div class="sticky top-0 z-10 -mx-4 sm:-mx-6 px-4 sm:px-6 py-3 mt-4 bg-gray-50/85 dark:bg-gray-900/85 backdrop-blur border-b border-gray-200 dark:border-gray-700">
    <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl shadow-sm p-3 sm:p-4 space-y-4">

      <!-- search + result count -->
      <div class="flex flex-wrap items-center gap-3">
        <div class="relative grow min-w-60">
          <div class="absolute inset-y-0 start-0 flex items-center ps-3 pointer-events-none text-gray-400 dark:text-gray-500">
            <svg class="size-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m21 21-4.35-4.35M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Z"/></svg>
          </div>
          <input id="q" type="text" placeholder="Search action, endpoint, domain or contributor…" autocomplete="off"
            class="block w-full py-2 ps-10 pe-9 rounded-lg text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200 placeholder:text-gray-400 dark:placeholder:text-gray-500 focus:border-indigo-500 focus:ring-indigo-500">
          <button id="q-clear" type="button" title="Clear search" class="hidden absolute inset-y-0 end-0 flex items-center pe-3 text-gray-400 hover:text-gray-700 dark:hover:text-gray-200">
            <svg class="size-4" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12"/></svg>
          </button>
        </div>
        <span id="count" class="inline-flex items-center gap-x-1.5 py-1.5 px-3 rounded-full text-xs font-medium bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 whitespace-nowrap"></span>
      </div>

      <hr class="border-gray-100 dark:border-gray-700">

      <!-- filters & controls -->
      <div class="flex flex-wrap gap-x-6 gap-y-4 items-end">

        <div class="flex flex-col gap-1.5">
          <label for="f-domain" class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Domain</label>
          <select id="f-domain" class="py-2 pe-9 ps-3 block rounded-lg text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200 focus:border-indigo-500 focus:ring-indigo-500"><option value="all">All domains</option></select>
        </div>

        <div class="flex flex-col gap-1.5">
          <label for="f-author" class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">PR author</label>
          <select id="f-author" class="py-2 pe-9 ps-3 block rounded-lg text-sm border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-800 dark:text-gray-200 focus:border-indigo-500 focus:ring-indigo-500"><option value="all">All PR authors</option></select>
        </div>

        <div class="flex flex-col gap-1.5">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Status</span>
          <div class="inline-flex rounded-lg shadow-sm" id="f-status">
            <button data-v="all" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 first:rounded-s-lg">All</button>
            <button data-v="implemented" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">Implemented</button>
            <button data-v="in_progress" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">In progress</button>
            <button data-v="missing" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px last:rounded-e-lg">Missing</button>
          </div>
        </div>

        <div class="flex flex-col gap-1.5">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Type</span>
          <div class="inline-flex rounded-lg shadow-sm" id="f-type">
            <button data-v="all" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 first:rounded-s-lg">All</button>
            <button data-v="Command" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">Commands</button>
            <button data-v="Query" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px last:rounded-e-lg">Queries</button>
          </div>
        </div>

        <div class="flex flex-col gap-1.5">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Min version</span>
          <div class="inline-flex rounded-lg shadow-sm" id="f-version">
            <button data-v="all" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 first:rounded-s-lg">All</button>
            <button data-v="9.0" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px" title="Buildable now (9.0.3 CI floor)">9.0.3+</button>
            <button data-v="9.1" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px" title="Needs 9.0.x dropped from the CI matrix">9.1+</button>
            <button data-v="dev" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">dev</button>
            <button data-v="na" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px last:rounded-e-lg">n/a</button>
          </div>
        </div>

        <div class="flex flex-col gap-1.5">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">Sort by</span>
          <div class="inline-flex rounded-lg shadow-sm" id="sort">
            <button data-k="name" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 first:rounded-s-lg">Name <span class="arr">▲</span></button>
            <button data-k="pct" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">Progress <span class="arr"></span></button>
            <button data-k="total" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">Total <span class="arr"></span></button>
            <button data-k="missing" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px">Missing <span class="arr"></span></button>
            <button data-k="prog" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px last:rounded-e-lg">In&nbsp;progress <span class="arr"></span></button>
          </div>
        </div>

        <div class="flex flex-col gap-1.5">
          <span class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500">View</span>
          <div class="inline-flex rounded-lg shadow-sm">
            <button id="expand" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 rounded-s-lg">Expand all</button>
            <button id="collapse" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700 -ms-px rounded-e-lg">Collapse all</button>
          </div>
        </div>

        <button id="reset" type="button" class="ms-auto self-end inline-flex items-center gap-1.5 py-1.5 px-3 text-sm font-medium rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 text-gray-600 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-gray-700">
          <svg class="size-3.5" xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4v3.5H16"/></svg>
          Reset
        </button>
      </div>
    </div>
  </div>

  <div id="list" class="mt-4 space-y-3"></div>
  <div id="empty" class="hide py-16 text-center text-gray-400 dark:text-gray-500">No endpoint matches the current filters.</div>
  </div><!-- /panel endpoints -->

  <!-- panel: statistics -->
  <div data-panel="stats" class="hide">

    <h2 class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mt-4 mb-2">Domains &amp; contributors</h2>
    <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-domains" class="text-2xl font-bold text-gray-900 dark:text-white">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Domains</div></div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-done" class="text-2xl font-bold text-teal-600 dark:text-teal-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Fully done (100%)</div></div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-empty" class="text-2xl font-bold text-red-600 dark:text-red-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Not started (0%)</div></div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-prs" class="text-2xl font-bold text-amber-500 dark:text-amber-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Open PRs</div></div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-contrib" class="text-2xl font-bold text-indigo-600 dark:text-indigo-400">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Contributors</div></div>
      <div class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl p-4 shadow-sm"><div id="m-split" class="text-2xl font-bold text-gray-900 dark:text-white">0</div><div class="text-xs uppercase tracking-wide text-gray-500 dark:text-gray-400 mt-0.5">Commands / Queries</div></div>
    </div>

    <h2 class="text-[11px] font-semibold uppercase tracking-wider text-gray-400 dark:text-gray-500 mt-6 mb-2">Top contributors <span class="normal-case font-normal text-gray-400 dark:text-gray-500">(merged + in&nbsp;progress)</span></h2>
    <div id="leaderboard" class="bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl overflow-x-auto shadow-sm"></div>
    <div class="flex flex-wrap gap-x-5 gap-y-1 mt-2 text-xs text-gray-500 dark:text-gray-400">
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-teal-500"></span> Implemented / merged</span>
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-amber-400"></span> In progress (open PR)</span>
    </div>
  </div>
</main>

<footer class="border-t border-gray-200 dark:border-gray-700 py-6 text-center text-xs text-gray-400 dark:text-gray-500">
  Generated __DATE__ from PrestaShop/PrestaShop#39630 with live PR verification against PrestaShop/ps_apiresources ·
  styled with <a class="text-blue-600 dark:text-blue-400 hover:underline" href="https://preline.co" target="_blank">Preline UI</a>.
</footer>

<script id="data" type="application/json">__DATA__</script>
<script src="https://cdn.jsdelivr.net/npm/preline@2/dist/preline.min.js"></script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const SL = {implemented:'Implemented', in_progress:'In progress', missing:'Missing'};
const SBADGE = {
  implemented:'bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300',
  in_progress:'bg-yellow-100 text-yellow-800 dark:bg-yellow-500/15 dark:text-yellow-300',
  missing:'bg-red-100 text-red-800 dark:bg-red-500/15 dark:text-red-300'
};
const SDOT = {implemented:'✅', in_progress:'🚧', missing:'❌'};
const SORD = {implemented:0, in_progress:1, missing:2};
const VL = {'9.0':'9.0.3+', '9.1':'9.1+', 'dev':'develop', 'na':'n/a'};
const VBADGE = {
  '9.0':'bg-emerald-100 text-emerald-800 dark:bg-emerald-500/15 dark:text-emerald-300',
  '9.1':'bg-amber-100 text-amber-800 dark:bg-amber-500/15 dark:text-amber-300',
  'dev':'bg-violet-100 text-violet-800 dark:bg-violet-500/15 dark:text-violet-300',
  'na':'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
};
const VTITLE = {
  '9.0':'Core CQRS class exists in 9.0.3 — buildable now (matches the ps_apiresources CI floor)',
  '9.1':'Introduced in 9.1 — buildable once 9.0.x is dropped from the CI matrix',
  'dev':'Only present on develop — future',
  'na':'CQRS class not located in core@develop (name mismatch, sub-domain, or non-CQRS row)'
};
const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let fStatus='all', fType='all', fQ='', fDomain='all', fAuthor='all', fVersion='all';
let sortKey='name', sortDir=1;
const prUrl = n => 'https://github.com/PrestaShop/ps_apiresources/pull/'+n;
const BADGE = 'inline-flex items-center gap-x-1 py-0.5 px-2 rounded-full text-xs font-medium';
const CODE = 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 px-1.5 py-0.5 rounded text-xs';
const LINK = 'text-blue-600 dark:text-blue-400 hover:underline';

function buildRow(r){
  const author = r.author || r.assignee || '';
  let who='';
  if(r.pr){
    const name = author ? '<a class="'+LINK+'" href="https://github.com/'+esc(author)+'" target="_blank">'+esc(author)+'</a> / ' : '';
    const merged = r.merged_pr ? ' <span class="'+BADGE+' bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300">merged</span>' : '';
    who = name+'<a class="'+LINK+'" href="'+prUrl(r.pr)+'" target="_blank">PR #'+r.pr+'</a>'+merged;
  } else if(r.in_code){
    const name = author ? '<a class="'+LINK+'" href="https://github.com/'+esc(author)+'" target="_blank">'+esc(author)+'</a> / ' : '';
    who = name+'<a class="'+BADGE+' bg-teal-100 text-teal-800 dark:bg-teal-500/15 dark:text-teal-300" href="https://github.com/PrestaShop/ps_apiresources/blob/dev/'+esc(r.src||'')+'" target="_blank" title="Already implemented in dev under a non-obvious path">in&nbsp;code</a>';
  }
  const ep = r.endpoint ? '<code class="'+CODE+'">'+esc(r.endpoint)+'</code>' : '<span class="text-gray-300 dark:text-gray-600">—</span>';
  const tb = r.type==='Command'?'bg-blue-100 text-blue-800 dark:bg-blue-500/15 dark:text-blue-300':'bg-purple-100 text-purple-800 dark:bg-purple-500/15 dark:text-purple-300';
  const ver = r.version || 'na';
  const vb = '<span class="'+BADGE+' '+VBADGE[ver]+'" title="'+esc(VTITLE[ver])+'">'+VL[ver]+'</span>';
  return '<tr class="row hover:bg-gray-50 dark:hover:bg-gray-700/40" data-s="'+r.status+'" data-t="'+r.type+'"'+
    ' data-action="'+esc(r.action.toLowerCase())+'" data-ep="'+esc((r.endpoint||'').toLowerCase())+'"'+
    ' data-author="'+esc(author)+'" data-sord="'+SORD[r.status]+'" data-version="'+ver+'"'+
    ' data-k="'+esc((r.action+' '+(r.endpoint||'')+' '+author).toLowerCase())+'">'+
    '<td class="py-2.5 px-4 align-top"><code class="'+CODE+'">'+esc(r.action)+'</code></td>'+
    '<td class="py-2.5 px-4 align-top"><span class="'+BADGE+' '+tb+'">'+r.type+'</span></td>'+
    '<td class="py-2.5 px-4 align-top">'+vb+'</td>'+
    '<td class="py-2.5 px-4 align-top"><span class="'+BADGE+' '+SBADGE[r.status]+'">'+SDOT[r.status]+' '+SL[r.status]+'</span></td>'+
    '<td class="py-2.5 px-4 align-top">'+ep+'</td>'+
    '<td class="py-2.5 px-4 align-top text-sm text-gray-600 dark:text-gray-300">'+(who||'<span class="text-gray-300 dark:text-gray-600">—</span>')+'</td></tr>';
}

function render(){
  const list=document.getElementById('list');
  let html='', opts='<option value="all">All domains</option>';
  const authors=new Set();
  for(const d of DATA.domains){
    const i=d.rows.filter(r=>r.status==='implemented').length;
    const p=d.rows.filter(r=>r.status==='in_progress').length;
    const m=d.rows.filter(r=>r.status==='missing').length;
    const t=d.rows.length, pct=i/t*100, dn=esc(d.name.toLowerCase());
    d.rows.forEach(r=>{const a=r.author||r.assignee; if(a) authors.add(a);});
    opts+='<option value="'+dn+'">'+esc(d.name)+' ('+i+'/'+t+')</option>';
    const rows=d.rows.map(buildRow).join('');
    html+='<div class="domain bg-white dark:bg-gray-800 border border-gray-200 dark:border-gray-700 rounded-xl overflow-hidden" data-dn="'+dn+'" data-name="'+dn+'" data-pct="'+pct.toFixed(2)+
      '" data-total="'+t+'" data-missing="'+m+'" data-prog="'+p+'" data-impl="'+i+'">'+
      '<div class="dh flex items-center gap-4 px-4 py-3 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700/50 select-none" onclick="this.parentNode.classList.toggle(\'col\')">'+
        '<span class="caret text-gray-400 text-xs">▼</span>'+
        '<span class="font-semibold text-gray-900 dark:text-gray-100">'+esc(d.name)+'</span>'+
        '<span class="grow"></span>'+
        '<div class="hidden sm:flex h-2.5 w-40 rounded-full bg-red-500 overflow-hidden shrink-0"><div class="bg-teal-500 h-full" style="width:'+(i/t*100)+'%"></div><div class="bg-amber-400 h-full" style="width:'+(p/t*100)+'%"></div></div>'+
        '<span class="text-sm text-gray-500 dark:text-gray-400 whitespace-nowrap w-24 text-right">'+i+'/'+t+' · '+Math.round(pct)+'%</span>'+
      '</div>'+
      '<div class="dbody overflow-x-auto border-t border-gray-100 dark:border-gray-700"><table class="w-full text-left">'+
        '<thead class="bg-gray-50 dark:bg-gray-800/60 text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wide sortable">'+
        '<tr>'+
          '<th data-c="action" class="py-2.5 px-4 font-semibold">Action<span class="arr"></span></th>'+
          '<th data-c="type" class="py-2.5 px-4 font-semibold">Type<span class="arr"></span></th>'+
          '<th data-c="version" class="py-2.5 px-4 font-semibold">Min&nbsp;ver<span class="arr"></span></th>'+
          '<th data-c="status" class="py-2.5 px-4 font-semibold">Status<span class="arr"></span></th>'+
          '<th data-c="ep" class="py-2.5 px-4 font-semibold">API Endpoint<span class="arr"></span></th>'+
          '<th class="py-2.5 px-4 font-semibold">Author / PR</th>'+
        '</tr></thead><tbody class="divide-y divide-gray-100 dark:divide-gray-700">'+rows+'</tbody></table></div></div>';
  }
  list.innerHTML=html;
  document.getElementById('f-domain').innerHTML=opts;
  document.getElementById('f-author').innerHTML='<option value="all">All PR authors</option>'+
    [...authors].sort((a,b)=>a.toLowerCase()<b.toLowerCase()?-1:1)
      .map(a=>'<option value="'+esc(a)+'">'+esc(a)+'</option>').join('');
}

function setStats(){
  const t=DATA.total,i=DATA.impl,p=DATA.prog,m=DATA.miss, $=id=>document.getElementById(id);
  $('c-total').textContent=t;$('c-impl').textContent=i;$('c-prog').textContent=p;$('c-miss').textContent=m;
  $('c-pct').textContent=(i/t*100).toFixed(1)+'%';
  $('c-proj').textContent=((i+p)/t*100).toFixed(1)+'%';
  $('ov-i').style.width=(i/t*100)+'%'; $('ov-p').style.width=(p/t*100)+'%';
  // domain & contributor KPIs computed from the dataset
  let done=0, empty=0, cmd=0, qry=0; const prs=new Set(), authors=new Set();
  for(const d of DATA.domains){
    const di=d.rows.filter(r=>r.status==='implemented').length;
    if(di===d.rows.length) done++;
    if(di===0) empty++;
    for(const r of d.rows){
      if(r.type==='Command') cmd++; else qry++;
      if(r.status==='in_progress' && r.pr) prs.add(r.pr);
      const a=r.author||r.assignee; if(a) authors.add(a);
    }
  }
  $('m-domains').textContent=DATA.domains.length;
  $('m-done').textContent=done;
  $('m-empty').textContent=empty;
  $('m-prs').textContent=prs.size;
  $('m-contrib').textContent=authors.size;
  $('m-split').textContent=cmd+' / '+qry;
}

function sortDomains(){
  const list=document.getElementById('list'), doms=[...list.querySelectorAll('.domain')];
  doms.sort((a,b)=>{
    if(sortKey==='name'){const av=a.dataset.name,bv=b.dataset.name;return av<bv?-sortDir:av>bv?sortDir:0;}
    const av=parseFloat(a.dataset[sortKey]),bv=parseFloat(b.dataset[sortKey]);
    if(av===bv)return a.dataset.name<b.dataset.name?-1:1;
    return (av-bv)*sortDir;
  });
  doms.forEach(d=>list.appendChild(d));
}

function applyFilter(){
  let shown=0;
  document.querySelectorAll('.domain').forEach(dom=>{
    const domOk = fDomain==='all' || dom.dataset.dn===fDomain;
    let vis=0;
    dom.querySelectorAll('tr.row').forEach(tr=>{
      const ok = domOk && (fStatus==='all'||tr.dataset.s===fStatus) && (fType==='all'||tr.dataset.t===fType)
        && (fAuthor==='all'||tr.dataset.author===fAuthor)
        && (fVersion==='all'||tr.dataset.version===fVersion)
        && (!fQ || tr.dataset.k.includes(fQ) || dom.dataset.dn.includes(fQ));
      tr.hidden=!ok; if(ok){vis++;shown++;}
    });
    dom.classList.toggle('hide', vis===0);
  });
  document.getElementById('count').textContent=shown+' endpoint'+(shown===1?'':'s')+' shown';
  document.getElementById('empty').classList.toggle('hide', shown!==0);
}

function sortTable(th){
  const col=th.dataset.c; if(!col) return;
  const table=th.closest('table'), tbody=table.querySelector('tbody');
  const dir = th.classList.contains('asc') ? -1 : 1;
  table.querySelectorAll('th').forEach(h=>{h.classList.remove('asc','desc');const a=h.querySelector('.arr');if(a)a.textContent='';});
  th.classList.add(dir===1?'asc':'desc'); th.querySelector('.arr').textContent=dir===1?'▲':'▼';
  const rows=[...tbody.querySelectorAll('tr.row')];
  rows.sort((a,b)=>{
    if(col==='status')return (+a.dataset.sord - +b.dataset.sord)*dir;
    const av=a.dataset[col]||'',bv=b.dataset[col]||'';return av<bv?-dir:av>bv?dir:0;
  });
  rows.forEach(r=>tbody.appendChild(r));
}

function segWire(id, set){
  document.querySelectorAll('#'+id+' button').forEach(b=>b.onclick=()=>{
    set(b);
    document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on');
  });
}

const qEl=document.getElementById('q'), qClear=document.getElementById('q-clear');
qEl.addEventListener('input',e=>{fQ=e.target.value.trim().toLowerCase();qClear.classList.toggle('hidden', !e.target.value);applyFilter();});
qClear.onclick=()=>{qEl.value='';fQ='';qClear.classList.add('hidden');applyFilter();qEl.focus();};
document.getElementById('f-domain').addEventListener('change',e=>{fDomain=e.target.value;applyFilter();});
document.getElementById('f-author').addEventListener('change',e=>{fAuthor=e.target.value;applyFilter();});
segWire('f-status', b=>{fStatus=b.dataset.v;applyFilter();});
segWire('f-type', b=>{fType=b.dataset.v;applyFilter();});
segWire('f-version', b=>{fVersion=b.dataset.v;applyFilter();});

function setSeg(id,val){document.querySelectorAll('#'+id+' button').forEach(b=>b.classList.toggle('on', b.dataset.v===val));}
document.getElementById('reset').onclick=()=>{
  fQ='';fDomain='all';fAuthor='all';fStatus='all';fType='all';fVersion='all';
  qEl.value='';qClear.classList.add('hidden');
  document.getElementById('f-domain').value='all';
  document.getElementById('f-author').value='all';
  setSeg('f-status','all');setSeg('f-type','all');setSeg('f-version','all');
  applyFilter();
};
document.querySelectorAll('#sort button').forEach(b=>b.onclick=()=>{
  const k=b.dataset.k;
  if(sortKey===k){sortDir*=-1;}else{sortKey=k;sortDir=(k==='name')?1:-1;}
  document.querySelectorAll('#sort button').forEach(x=>{x.classList.remove('on');x.querySelector('.arr').textContent='';});
  b.classList.add('on');b.querySelector('.arr').textContent=sortDir===1?'▲':'▼';
  sortDomains();
});
document.getElementById('expand').onclick=()=>document.querySelectorAll('.domain').forEach(d=>d.classList.remove('col'));
document.getElementById('collapse').onclick=()=>document.querySelectorAll('.domain').forEach(d=>d.classList.add('col'));
document.addEventListener('click',e=>{const th=e.target.closest('th[data-c]');if(th)sortTable(th);});

// dark mode toggle (persisted; system preference honoured on first load via the <head> script)
const themeBtn=document.getElementById('theme');
function syncTheme(){themeBtn.textContent=document.documentElement.classList.contains('dark')?'☀️':'🌙';}
themeBtn.onclick=()=>{const d=document.documentElement.classList.toggle('dark');try{localStorage.setItem('theme',d?'dark':'light');}catch(e){}syncTheme();};
syncTheme();

// Statistics tab: contributor leaderboard (counts everything — merged, in progress, discovered)
function renderStats(){
  const stat={};
  for(const d of DATA.domains) for(const r of d.rows){
    const a=r.author||r.assignee; if(!a) continue;
    const s=stat[a]||(stat[a]={impl:0,prog:0,prs:new Set()});
    if(r.status==='implemented') s.impl++; else if(r.status==='in_progress') s.prog++;
    if(r.pr) s.prs.add(r.pr);
  }
  const list=Object.entries(stat).map(([a,s])=>({a,impl:s.impl,prog:s.prog,total:s.impl+s.prog,prs:s.prs.size}))
    .sort((x,y)=>y.total-x.total||y.prs-x.prs||(x.a.toLowerCase()<y.a.toLowerCase()?-1:1));
  const max=Math.max(1,...list.map(x=>x.total));
  const body=list.map((x,idx)=>{
    const medal=idx===0?'🥇':idx===1?'🥈':idx===2?'🥉':'#'+(idx+1);
    return '<tr class="border-t border-gray-100 dark:border-gray-700">'+
      '<td class="py-2 px-4 text-sm text-gray-500 dark:text-gray-400 w-12 text-center">'+medal+'</td>'+
      '<td class="py-2 px-4 whitespace-nowrap"><a class="inline-flex items-center gap-2 font-medium '+LINK+'" href="https://github.com/'+esc(x.a)+'" target="_blank">'+
        '<img class="size-6 rounded-full bg-gray-200 dark:bg-gray-700" loading="lazy" src="https://github.com/'+esc(x.a)+'.png?size=48" alt="">'+esc(x.a)+'</a></td>'+
      '<td class="py-2 px-4 w-1/2 min-w-48"><div class="flex items-center gap-2">'+
        '<div class="grow flex h-2.5 rounded-full bg-gray-200 dark:bg-gray-700 overflow-hidden">'+
          '<div class="bg-teal-500 h-full" style="width:'+(x.impl/max*100)+'%"></div><div class="bg-amber-400 h-full" style="width:'+(x.prog/max*100)+'%"></div></div>'+
        '<span class="text-sm tabular-nums font-medium text-gray-700 dark:text-gray-200 w-8 text-right">'+x.total+'</span></div></td>'+
      '<td class="py-2 px-4 text-sm tabular-nums text-teal-600 dark:text-teal-400 text-right">'+x.impl+'</td>'+
      '<td class="py-2 px-4 text-sm tabular-nums text-amber-600 dark:text-amber-400 text-right">'+x.prog+'</td>'+
      '<td class="py-2 px-4 text-sm tabular-nums text-gray-500 dark:text-gray-400 text-right">'+x.prs+'</td></tr>';
  }).join('');
  document.getElementById('leaderboard').innerHTML=
    '<table class="w-full text-left"><thead class="bg-gray-50 dark:bg-gray-800/60 text-gray-500 dark:text-gray-400 text-xs uppercase tracking-wide">'+
    '<tr><th class="py-2.5 px-4 font-semibold w-12 text-center">#</th><th class="py-2.5 px-4 font-semibold">Contributor</th>'+
    '<th class="py-2.5 px-4 font-semibold">Endpoints</th>'+
    '<th class="py-2.5 px-4 font-semibold text-right">Done</th>'+
    '<th class="py-2.5 px-4 font-semibold text-right">In&nbsp;prog.</th>'+
    '<th class="py-2.5 px-4 font-semibold text-right">PRs</th></tr></thead><tbody>'+body+'</tbody></table>';
}

// tabs
function showTab(name){
  document.querySelectorAll('[data-panel]').forEach(p=>p.classList.toggle('hide', p.dataset.panel!==name));
  document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('tab-on', b.dataset.tab===name));
}
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));

setStats();render();sortDomains();applyFilter();renderStats();showTab('endpoints');
if(window.HSStaticMethods) window.HSStaticMethods.autoInit();
</script>
</body>
</html>'''


def main():
    body = fetch_issue_body()
    domains = parse(body)
    annotate_versions(domains)
    referenced = {r['pr'] for d in domains for r in d['rows'] if r['pr']}
    _info, merged, closed = reconcile(domains)
    in_code = discover_implemented_in_code(domains)
    discovered = discover_unlisted_prs(domains, referenced)
    note = ""
    if merged:
        items = ", ".join(f"#{pr} ({dom} <code>{act}</code>)" for dom, act, pr in merged)
        note = f"<b>Merged since last issue snapshot:</b> {items}."
    if closed:
        note += " " + f"<b>Reverted to Missing (PR closed):</b> " + \
                ", ".join(f"{dom} <code>{act}</code>" for dom, act in closed) + "."
    if discovered:
        prs = sorted({pr for pr, _, _ in discovered})
        note += " " + f"<b>Discovered {len(prs)} open PR(s) not yet in the issue:</b> " + \
                ", ".join(f"#{p}" for p in prs) + "."
    if in_code:
        acts = ", ".join(f"{dom} <code>{act}</code>" for dom, act, _ in in_code)
        note += " " + f"<b>Found {len(in_code)} already implemented in dev (issue still listed Missing):</b> " + acts + "."
    if not note:
        note = "No PR status changes since the source snapshot."
    miss_ver = {}
    for d in domains:
        for r in d['rows']:
            if r['status'] == 'missing':
                v = r.get('version') or 'n/a'
                miss_ver[v] = miss_ver.get(v, 0) + 1
    parts = []
    for lbl, txt in (('9.0', 'buildable now (9.0.3)'), ('9.1', 'need 9.1'),
                     ('dev', 'develop-only'), ('n/a', 'class not located')):
        if miss_ver.get(lbl):
            parts.append(f"{miss_ver[lbl]} {txt}")
    if parts:
        note += " <b>Missing endpoints by min version:</b> " + ", ".join(parts) + "."
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    html, (total, impl, prog, miss) = build(domains, stamp, note)
    with open(OUT, "w") as f:
        f.write(html)
    print(f"[{stamp}] wrote {OUT}")
    print(f"  domains={len(domains)} total={total} impl={impl} prog={prog} miss={miss} "
          f"({impl/total*100:.1f}% done, {(impl+prog)/total*100:.1f}% projected)")
    vc = {}
    miss_by_ver = {}
    for d in domains:
        for r in d['rows']:
            v = r.get('version') or 'n/a'
            vc[v] = vc.get(v, 0) + 1
            if r['status'] == 'missing':
                miss_by_ver[v] = miss_by_ver.get(v, 0) + 1
    print("  min-version split:", {k: vc[k] for k in sorted(vc)})
    print("  MISSING by min-version (9.0 = buildable now):",
          {k: miss_by_ver[k] for k in sorted(miss_by_ver)})
    if merged:
        print("  newly merged:", merged)
    if closed:
        print("  reverted (closed):", closed)
    if discovered:
        print("  discovered unlisted PRs:", discovered)
    if in_code:
        print("  found implemented in dev code:", in_code)


if __name__ == "__main__":
    main()
