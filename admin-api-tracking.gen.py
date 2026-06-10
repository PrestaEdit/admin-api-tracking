#!/usr/bin/env python3
"""
Self-contained generator for the PrestaShop Admin API endpoint tracking page.

Pipeline (no manual steps):
  1. Fetch issue #39630 body from PrestaShop/PrestaShop via `gh`.
  2. Parse the per-domain markdown tables into structured rows.
  3. Collect every referenced ps_apiresources PR and query its live state via `gh`.
  4. Reconcile statuses:  PR merged -> Implemented,  PR closed (not merged) -> Missing,
     PR still open -> In Progress.  The verified PR author is attached to each row.
  5. Render a standalone, interactive HTML file styled with Preline UI / Tailwind
     (search / filters by status, type, domain, author / sorts).

Requirements at runtime: python3 + an authenticated `gh` CLI.
Usage: python3 admin-api-tracking.gen.py [output.html]
"""
import json, re, subprocess, sys, os
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
<style>
  /* behaviour-only rules kept in CSS so the proven JS hooks stay intact */
  .hide{display:none!important}
  tr.row[hidden]{display:none}
  .domain.col .dbody{display:none}
  .domain.col .caret{transform:rotate(-90deg)}
  .caret{transition:transform .15s ease}
  .seg-btn.on{background-color:#1f2937;color:#fff;border-color:#1f2937;z-index:1}
  .sortable .arr{font-size:9px;opacity:.6}
  [data-c]{cursor:pointer;user-select:none}
</style>
</head>
<body class="bg-gray-50 text-gray-800 antialiased">

<header class="bg-white border-b border-gray-200">
  <div class="max-w-7xl mx-auto px-4 sm:px-6 py-6">
    <h1 class="text-2xl font-bold text-gray-900">PrestaShop Admin API — Endpoint Tracking</h1>
    <p class="mt-1 text-sm text-gray-500">
      CQRS Commands &amp; Queries mapped to Admin API endpoints ·
      source <a class="text-blue-600 hover:underline" href="https://github.com/PrestaShop/PrestaShop/issues/39630" target="_blank">issue #39630</a> ·
      endpoints live in <a class="text-blue-600 hover:underline" href="https://github.com/PrestaShop/ps_apiresources" target="_blank">ps_apiresources</a>
    </p>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 sm:px-6 pb-20">

  <!-- stat cards -->
  <div class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3 mt-6">
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-total" class="text-2xl font-bold text-gray-900">0</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">Total</div></div>
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-impl" class="text-2xl font-bold text-teal-600">0</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">Implemented</div></div>
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-prog" class="text-2xl font-bold text-amber-500">0</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">In progress</div></div>
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-miss" class="text-2xl font-bold text-red-600">0</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">Missing</div></div>
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-pct" class="text-2xl font-bold text-indigo-600">0%</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">Progress</div></div>
    <div class="bg-white border border-gray-200 rounded-xl p-4 shadow-sm"><div id="c-proj" class="text-2xl font-bold text-indigo-400">0%</div><div class="text-xs uppercase tracking-wide text-gray-500 mt-0.5">Projected</div></div>
  </div>

  <!-- overall progress -->
  <div class="mt-4">
    <div class="flex h-3 rounded-full bg-gray-200 overflow-hidden">
      <div id="ov-i" class="bg-teal-500 h-full"></div>
      <div id="ov-p" class="bg-amber-400 h-full"></div>
    </div>
    <div class="flex flex-wrap gap-x-5 gap-y-1 mt-2 text-xs text-gray-500">
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-teal-500"></span> Implemented</span>
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-amber-400"></span> In progress (open PR)</span>
      <span class="inline-flex items-center gap-1.5"><span class="size-2.5 rounded-sm bg-red-500"></span> Missing</span>
    </div>
  </div>

  <!-- info note (Preline soft alert) -->
  <div class="mt-4 bg-blue-50 border border-blue-200 border-s-4 border-s-blue-500 text-blue-800 rounded-lg p-4 text-sm">
    <b>Auto-generated __DATE__.</b> Built live from issue #39630, with every referenced
    <code class="bg-white/60 px-1 rounded">ps_apiresources</code> PR re-checked against GitHub (merged → Implemented, closed → Missing, open → In&nbsp;Progress),
    and the verified PR author attached. Exact-duplicate source rows are de-duplicated. __MERGEDNOTE__
  </div>

  <!-- controls -->
  <div class="sticky top-0 z-10 -mx-4 sm:-mx-6 px-4 sm:px-6 py-3 mt-6 bg-gray-50/85 backdrop-blur border-b border-gray-200 space-y-3">
    <div class="flex flex-wrap gap-2 items-center">
      <input id="q" placeholder="Search action, endpoint, domain or contributor…" autocomplete="off"
        class="grow min-w-56 py-2 px-3 block border border-gray-200 rounded-lg text-sm focus:border-blue-500 focus:ring-blue-500">
      <select id="f-domain" class="py-2 pe-9 ps-3 block border border-gray-200 rounded-lg text-sm focus:border-blue-500 focus:ring-blue-500"><option value="all">All domains</option></select>
      <select id="f-author" class="py-2 pe-9 ps-3 block border border-gray-200 rounded-lg text-sm focus:border-blue-500 focus:ring-blue-500"><option value="all">All PR authors</option></select>
      <span class="ms-auto text-sm text-gray-500" id="count"></span>
    </div>
    <div class="flex flex-wrap gap-x-4 gap-y-2 items-center">
      <div class="flex items-center gap-2">
        <span class="text-xs uppercase tracking-wide text-gray-400">Status</span>
        <div class="inline-flex rounded-lg shadow-sm" id="f-status">
          <button data-v="all" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px first:ms-0 first:rounded-s-lg last:rounded-e-lg">All</button>
          <button data-v="implemented" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">Implemented</button>
          <button data-v="in_progress" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">In progress</button>
          <button data-v="missing" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px last:rounded-e-lg">Missing</button>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-xs uppercase tracking-wide text-gray-400">Type</span>
        <div class="inline-flex rounded-lg shadow-sm" id="f-type">
          <button data-v="all" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 first:rounded-s-lg">All</button>
          <button data-v="Command" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">Commands</button>
          <button data-v="Query" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px last:rounded-e-lg">Queries</button>
        </div>
      </div>
      <div class="flex items-center gap-2">
        <span class="text-xs uppercase tracking-wide text-gray-400">Sort</span>
        <div class="inline-flex rounded-lg shadow-sm" id="sort">
          <button data-k="name" class="seg-btn on py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 first:rounded-s-lg">Name <span class="arr">▲</span></button>
          <button data-k="pct" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">Progress <span class="arr"></span></button>
          <button data-k="total" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">Total <span class="arr"></span></button>
          <button data-k="missing" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px">Missing <span class="arr"></span></button>
          <button data-k="prog" class="seg-btn py-1.5 px-3 text-sm font-medium border border-gray-200 bg-white text-gray-700 hover:bg-gray-50 -ms-px last:rounded-e-lg">In&nbsp;progress <span class="arr"></span></button>
        </div>
      </div>
      <button id="expand" class="py-1.5 px-3 text-sm font-medium rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50">Expand all</button>
      <button id="collapse" class="py-1.5 px-3 text-sm font-medium rounded-lg border border-gray-200 bg-white text-gray-700 hover:bg-gray-50">Collapse all</button>
    </div>
  </div>

  <div id="list" class="mt-4 space-y-3"></div>
  <div id="empty" class="hide py-16 text-center text-gray-400">No endpoint matches the current filters.</div>
</main>

<footer class="border-t border-gray-200 py-6 text-center text-xs text-gray-400">
  Generated __DATE__ from PrestaShop/PrestaShop#39630 with live PR verification against PrestaShop/ps_apiresources ·
  styled with <a class="text-blue-600 hover:underline" href="https://preline.co" target="_blank">Preline UI</a>.
</footer>

<script id="data" type="application/json">__DATA__</script>
<script src="https://cdn.jsdelivr.net/npm/preline@2/dist/preline.min.js"></script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const SL = {implemented:'Implemented', in_progress:'In progress', missing:'Missing'};
const SBADGE = {implemented:'bg-teal-100 text-teal-800', in_progress:'bg-yellow-100 text-yellow-800', missing:'bg-red-100 text-red-800'};
const SDOT = {implemented:'✅', in_progress:'🚧', missing:'❌'};
const SORD = {implemented:0, in_progress:1, missing:2};
const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let fStatus='all', fType='all', fQ='', fDomain='all', fAuthor='all';
let sortKey='name', sortDir=1;
const prUrl = n => 'https://github.com/PrestaShop/ps_apiresources/pull/'+n;
const BADGE = 'inline-flex items-center gap-x-1 py-0.5 px-2 rounded-full text-xs font-medium';

function buildRow(r){
  const author = r.author || r.assignee || '';
  let who='';
  if(r.pr){
    const name = author ? '<a class="text-blue-600 hover:underline" href="https://github.com/'+esc(author)+'" target="_blank">'+esc(author)+'</a> / ' : '';
    const merged = r.merged_pr ? ' <span class="'+BADGE+' bg-teal-100 text-teal-800">merged</span>' : '';
    who = name+'<a class="text-blue-600 hover:underline" href="'+prUrl(r.pr)+'" target="_blank">PR #'+r.pr+'</a>'+merged;
  }
  const ep = r.endpoint ? '<code class="bg-gray-100 text-gray-700 px-1.5 py-0.5 rounded text-xs">'+esc(r.endpoint)+'</code>' : '<span class="text-gray-300">—</span>';
  const tb = r.type==='Command'?'bg-blue-100 text-blue-800':'bg-purple-100 text-purple-800';
  return '<tr class="row hover:bg-gray-50" data-s="'+r.status+'" data-t="'+r.type+'"'+
    ' data-action="'+esc(r.action.toLowerCase())+'" data-ep="'+esc((r.endpoint||'').toLowerCase())+'"'+
    ' data-author="'+esc(author)+'" data-sord="'+SORD[r.status]+'"'+
    ' data-k="'+esc((r.action+' '+(r.endpoint||'')+' '+author).toLowerCase())+'">'+
    '<td class="py-2.5 px-4 align-top"><code class="bg-gray-100 text-gray-700 px-1.5 py-0.5 rounded text-xs">'+esc(r.action)+'</code></td>'+
    '<td class="py-2.5 px-4 align-top"><span class="'+BADGE+' '+tb+'">'+r.type+'</span></td>'+
    '<td class="py-2.5 px-4 align-top"><span class="'+BADGE+' '+SBADGE[r.status]+'">'+SDOT[r.status]+' '+SL[r.status]+'</span></td>'+
    '<td class="py-2.5 px-4 align-top">'+ep+'</td>'+
    '<td class="py-2.5 px-4 align-top text-sm text-gray-600">'+(who||'<span class="text-gray-300">—</span>')+'</td></tr>';
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
    html+='<div class="domain bg-white border border-gray-200 rounded-xl overflow-hidden" data-dn="'+dn+'" data-name="'+dn+'" data-pct="'+pct.toFixed(2)+
      '" data-total="'+t+'" data-missing="'+m+'" data-prog="'+p+'" data-impl="'+i+'">'+
      '<div class="dh flex items-center gap-4 px-4 py-3 cursor-pointer hover:bg-gray-50 select-none" onclick="this.parentNode.classList.toggle(\'col\')">'+
        '<span class="caret text-gray-400 text-xs">▼</span>'+
        '<span class="font-semibold text-gray-900">'+esc(d.name)+'</span>'+
        '<span class="grow"></span>'+
        '<div class="hidden sm:flex h-2.5 w-40 rounded-full bg-gray-200 overflow-hidden shrink-0"><div class="bg-teal-500 h-full" style="width:'+(i/t*100)+'%"></div><div class="bg-amber-400 h-full" style="width:'+(p/t*100)+'%"></div></div>'+
        '<span class="text-sm text-gray-500 whitespace-nowrap w-24 text-right">'+i+'/'+t+' · '+Math.round(pct)+'%</span>'+
      '</div>'+
      '<div class="dbody overflow-x-auto border-t border-gray-100"><table class="w-full text-left">'+
        '<thead class="bg-gray-50 text-gray-500 text-xs uppercase tracking-wide sortable">'+
        '<tr>'+
          '<th data-c="action" class="py-2.5 px-4 font-semibold">Action<span class="arr"></span></th>'+
          '<th data-c="type" class="py-2.5 px-4 font-semibold">Type<span class="arr"></span></th>'+
          '<th data-c="status" class="py-2.5 px-4 font-semibold">Status<span class="arr"></span></th>'+
          '<th data-c="ep" class="py-2.5 px-4 font-semibold">API Endpoint<span class="arr"></span></th>'+
          '<th class="py-2.5 px-4 font-semibold">Author / PR</th>'+
        '</tr></thead><tbody class="divide-y divide-gray-100">'+rows+'</tbody></table></div></div>';
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

document.getElementById('q').addEventListener('input',e=>{fQ=e.target.value.trim().toLowerCase();applyFilter();});
document.getElementById('f-domain').addEventListener('change',e=>{fDomain=e.target.value;applyFilter();});
document.getElementById('f-author').addEventListener('change',e=>{fAuthor=e.target.value;applyFilter();});
segWire('f-status', b=>{fStatus=b.dataset.v;applyFilter();});
segWire('f-type', b=>{fType=b.dataset.v;applyFilter();});
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

setStats();render();sortDomains();applyFilter();
if(window.HSStaticMethods) window.HSStaticMethods.autoInit();
</script>
</body>
</html>'''


def main():
    body = fetch_issue_body()
    domains = parse(body)
    _info, merged, closed = reconcile(domains)
    note = ""
    if merged:
        items = ", ".join(f"#{pr} ({dom} <code>{act}</code>)" for dom, act, pr in merged)
        note = f"<b>Merged since last issue snapshot:</b> {items}."
    if closed:
        note += " " + f"<b>Reverted to Missing (PR closed):</b> " + \
                ", ".join(f"{dom} <code>{act}</code>" for dom, act in closed) + "."
    if not note:
        note = "No PR status changes since the source snapshot."
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    html, (total, impl, prog, miss) = build(domains, stamp, note)
    with open(OUT, "w") as f:
        f.write(html)
    print(f"[{stamp}] wrote {OUT}")
    print(f"  domains={len(domains)} total={total} impl={impl} prog={prog} miss={miss} "
          f"({impl/total*100:.1f}% done, {(impl+prog)/total*100:.1f}% projected)")
    if merged:
        print("  newly merged:", merged)
    if closed:
        print("  reverted (closed):", closed)


if __name__ == "__main__":
    main()
