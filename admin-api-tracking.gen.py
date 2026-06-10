#!/usr/bin/env python3
"""
Self-contained generator for the PrestaShop Admin API endpoint tracking page.

Pipeline (no manual steps):
  1. Fetch issue #39630 body from PrestaShop/PrestaShop via `gh`.
  2. Parse the per-domain markdown tables into structured rows.
  3. Collect every referenced ps_apiresources PR and query its live state via `gh`.
  4. Reconcile statuses:  PR merged -> Implemented,  PR closed (not merged) -> Missing,
     PR still open -> In Progress.
  5. Render a standalone, interactive HTML file (search / filters / sorts).

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
                                'endpoint': endpoint, 'assignee': assignee, 'pr': pr})
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
                r['assignee'] = author  # verified real PR creator (overrides issue listing)
            if st == 'MERGED':
                r['status'] = 'implemented'
                r['merged_pr'] = r['pr']
                r['assignee'] = None
                if not r['endpoint']:
                    r['endpoint'] = ''  # endpoint detail not auto-derived; PR link kept
                moved_merged.append((d['name'], r['action'], r['pr']))
            elif st == 'CLOSED':
                r['status'] = 'missing'
                r['endpoint'] = ''
                r['assignee'] = r['pr'] = None
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
<style>
:root{--bg:#0f1421;--panel:#171e2e;--panel2:#1d2638;--line:#2a3550;--txt:#e7ecf5;--muted:#93a0bd;
--green:#2ecc71;--greenbg:#10351f;--amber:#f5a623;--amberbg:#3a2c0c;--red:#e05260;--redbg:#3a1419;--accent:#5b8def;--code:#0b0f1a;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--txt);font-size:14px;line-height:1.45}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header{padding:28px 32px 18px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#141b2b,#0f1421)}
h1{margin:0 0 4px;font-size:22px;font-weight:700}
.sub{color:var(--muted);font-size:13px}.sub code{background:var(--code);padding:1px 6px;border-radius:4px}
.wrap{padding:22px 32px 60px;max-width:1280px;margin:0 auto}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0 8px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card .n{font-size:26px;font-weight:700}.card .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:2px}
.card.impl .n{color:var(--green)}.card.prog .n{color:var(--amber)}.card.miss .n{color:var(--red)}
.bar{height:14px;border-radius:8px;background:var(--panel2);overflow:hidden;display:flex;border:1px solid var(--line)}
.bar i{display:block;height:100%}.bar .si{background:var(--green)}.bar .sp{background:var(--amber)}
.overall{margin:14px 0 4px}.overall .bar{height:20px}
.legendline{display:flex;gap:18px;flex-wrap:wrap;color:var(--muted);font-size:12px;margin-top:8px}
.legendline span{display:inline-flex;align-items:center;gap:6px}
.dot{width:10px;height:10px;border-radius:3px;display:inline-block}
.dot.i{background:var(--green)}.dot.p{background:var(--amber)}.dot.m{background:var(--red)}
.controls{position:sticky;top:0;background:var(--bg);padding:10px 0 8px;z-index:5;border-bottom:1px solid var(--line);margin:22px 0 6px;display:flex;flex-direction:column;gap:9px}
.crow{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input#q{flex:1;min-width:220px;background:var(--panel);border:1px solid var(--line);color:var(--txt);padding:9px 12px;border-radius:8px;font-size:14px}
select{background:var(--panel);border:1px solid var(--line);color:var(--txt);padding:8px 10px;border-radius:8px;font-size:13px;cursor:pointer}
.lbl{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.seg button{background:var(--panel);color:var(--muted);border:0;padding:8px 12px;cursor:pointer;font-size:13px;display:inline-flex;align-items:center;gap:5px}
.seg button.on{background:var(--accent);color:#fff}.seg button .arr{font-size:10px;opacity:.85}
.btn{background:var(--panel);color:var(--muted);border:1px solid var(--line);padding:8px 12px;border-radius:8px;cursor:pointer;font-size:13px}
.btn:hover{color:var(--txt)}
.count{color:var(--muted);font-size:13px;margin-left:auto}
.domain{background:var(--panel);border:1px solid var(--line);border-radius:10px;margin-top:14px;overflow:hidden}
.dh{display:flex;align-items:center;gap:14px;padding:12px 16px;cursor:pointer;user-select:none}
.dh:hover{background:var(--panel2)}
.dh .caret{color:var(--muted);transition:transform .15s}.domain.col .caret{transform:rotate(-90deg)}
.dh .dn{font-weight:700;font-size:15px}
.dh .dp{color:var(--muted);font-size:13px;white-space:nowrap;min-width:92px;text-align:right}
.dh .bar{width:160px;flex-shrink:0}.dh .spacer{flex:1}
table{width:100%;border-collapse:collapse}.domain.col table{display:none}
th,td{text-align:left;padding:9px 16px;border-top:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.5px;font-weight:600;background:var(--panel2);cursor:pointer;user-select:none;white-space:nowrap}
th .arr{font-size:9px;margin-left:4px;opacity:.6}
td code{background:var(--code);padding:2px 6px;border-radius:4px;font-size:12.5px}
.badge{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11.5px;font-weight:600;white-space:nowrap}
.b-impl{background:var(--greenbg);color:var(--green)}.b-prog{background:var(--amberbg);color:var(--amber)}.b-miss{background:var(--redbg);color:var(--red)}
.b-cmd{background:#1b2f4d;color:#7db0ff}.b-qry{background:#3a2348;color:#d39bf0}
tr.row[hidden]{display:none}.ep{color:var(--muted)}.hide{display:none!important}
footer{color:var(--muted);font-size:12px;padding:24px 32px;border-top:1px solid var(--line);text-align:center}
.note{background:var(--panel2);border:1px solid var(--line);border-left:3px solid var(--accent);border-radius:8px;padding:12px 16px;margin-top:16px;color:#c4cfe6;font-size:13px}
.note b{color:var(--txt)}.empty{padding:40px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<header>
  <h1>PrestaShop Admin API — Endpoint Tracking</h1>
  <div class="sub">CQRS Commands &amp; Queries mapped to Admin API endpoints &middot;
    source <a href="https://github.com/PrestaShop/PrestaShop/issues/39630" target="_blank">issue #39630</a> &middot;
    endpoints live in <a href="https://github.com/PrestaShop/ps_apiresources" target="_blank">ps_apiresources</a></div>
</header>
<div class="wrap">
  <div class="cards">
    <div class="card"><div class="n" id="c-total">0</div><div class="l">Total endpoints</div></div>
    <div class="card impl"><div class="n" id="c-impl">0</div><div class="l">Implemented</div></div>
    <div class="card prog"><div class="n" id="c-prog">0</div><div class="l">In progress</div></div>
    <div class="card miss"><div class="n" id="c-miss">0</div><div class="l">Missing</div></div>
    <div class="card"><div class="n" id="c-pct">0%</div><div class="l">Progress</div></div>
    <div class="card"><div class="n" id="c-proj">0%</div><div class="l">Projected (PRs merged)</div></div>
  </div>
  <div class="overall">
    <div class="bar"><i class="si" id="ov-i"></i><i class="sp" id="ov-p"></i></div>
    <div class="legendline">
      <span><i class="dot i"></i> Implemented</span>
      <span><i class="dot p"></i> In progress (open PR)</span>
      <span><i class="dot m"></i> Missing</span>
    </div>
  </div>
  <div class="note">
    <b>Auto-generated __DATE__.</b> Built live from issue #39630, with every referenced
    <code>ps_apiresources</code> PR re-checked against GitHub (merged &rarr; Implemented, closed &rarr; Missing, open &rarr; In&nbsp;Progress).
    Exact-duplicate rows from the source table are de-duplicated. __MERGEDNOTE__
  </div>
  <div class="controls">
    <div class="crow">
      <input id="q" placeholder="Search action, endpoint, domain or contributor…" autocomplete="off">
      <select id="f-domain"><option value="all">All domains</option></select>
      <span class="count" id="count"></span>
    </div>
    <div class="crow">
      <span class="lbl">Status</span>
      <div class="seg" id="f-status">
        <button data-v="all" class="on">All</button>
        <button data-v="implemented">Implemented</button>
        <button data-v="in_progress">In progress</button>
        <button data-v="missing">Missing</button>
      </div>
      <span class="lbl">Type</span>
      <div class="seg" id="f-type">
        <button data-v="all" class="on">All</button>
        <button data-v="Command">Commands</button>
        <button data-v="Query">Queries</button>
      </div>
    </div>
    <div class="crow">
      <span class="lbl">Sort domains</span>
      <div class="seg" id="sort">
        <button data-k="name" class="on">Name <span class="arr">▲</span></button>
        <button data-k="pct">Progress&nbsp;% <span class="arr"></span></button>
        <button data-k="total">Total <span class="arr"></span></button>
        <button data-k="missing">Missing <span class="arr"></span></button>
        <button data-k="prog">In&nbsp;progress <span class="arr"></span></button>
      </div>
      <button class="btn" id="expand">Expand all</button>
      <button class="btn" id="collapse">Collapse all</button>
    </div>
  </div>
  <div id="list"></div>
  <div class="empty hide" id="empty">No endpoint matches the current filters.</div>
</div>
<footer>Generated __DATE__ from PrestaShop/PrestaShop#39630 with live PR verification against PrestaShop/ps_apiresources. Static file — no network calls.</footer>

<script id="data" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('data').textContent);
const SB = {implemented:'b-impl', in_progress:'b-prog', missing:'b-miss'};
const SL = {implemented:'✅ Implemented', in_progress:'🚧 In Progress', missing:'❌ Missing'};
const SORD = {implemented:0, in_progress:1, missing:2};
const esc = s => (s||'').replace(/[&<>"]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let fStatus='all', fType='all', fQ='', fDomain='all';
let sortKey='name', sortDir=1;
const prUrl = n => 'https://github.com/PrestaShop/ps_apiresources/pull/'+n;

function buildRow(r){
  let assignee='';
  if(r.pr){
    const who = r.assignee ? '<a href="https://github.com/'+esc(r.assignee)+'" target="_blank">'+esc(r.assignee)+'</a> / ' : '';
    const merged = r.merged_pr ? ' <span class="badge b-impl" style="font-size:10px">merged</span>' : '';
    assignee = who+'<a href="'+prUrl(r.pr)+'" target="_blank">PR #'+r.pr+'</a>'+merged;
  }
  const ep = r.endpoint ? '<code>'+esc(r.endpoint)+'</code>' : '<span class="ep">—</span>';
  const tb = r.type==='Command'?'b-cmd':'b-qry';
  return '<tr class="row" data-s="'+r.status+'" data-t="'+r.type+'"'+
    ' data-action="'+esc(r.action.toLowerCase())+'" data-ep="'+esc((r.endpoint||'').toLowerCase())+'"'+
    ' data-sord="'+SORD[r.status]+'" data-k="'+esc((r.action+' '+(r.endpoint||'')+' '+(r.assignee||'')).toLowerCase())+'">'+
    '<td><code>'+esc(r.action)+'</code></td>'+
    '<td><span class="badge '+tb+'">'+r.type+'</span></td>'+
    '<td><span class="badge '+SB[r.status]+'">'+SL[r.status]+'</span></td>'+
    '<td>'+ep+'</td>'+
    '<td>'+(assignee||'<span class="ep">—</span>')+'</td></tr>';
}

function render(){
  const list=document.getElementById('list'), dsel=document.getElementById('f-domain');
  let html='', opts='<option value="all">All domains</option>';
  for(const d of DATA.domains){
    const i=d.rows.filter(r=>r.status==='implemented').length;
    const p=d.rows.filter(r=>r.status==='in_progress').length;
    const m=d.rows.filter(r=>r.status==='missing').length;
    const t=d.rows.length, pct=i/t*100, dn=esc(d.name.toLowerCase());
    opts+='<option value="'+dn+'">'+esc(d.name)+' ('+i+'/'+t+')</option>';
    const rows=d.rows.map(buildRow).join('');
    html+='<div class="domain" data-dn="'+dn+'" data-name="'+dn+'" data-pct="'+pct.toFixed(2)+
      '" data-total="'+t+'" data-missing="'+m+'" data-prog="'+p+'" data-impl="'+i+'">'+
      '<div class="dh" onclick="this.parentNode.classList.toggle(\'col\')">'+
        '<span class="caret">▼</span><span class="dn">'+esc(d.name)+'</span><span class="spacer"></span>'+
        '<div class="bar"><i class="si" style="width:'+(i/t*100)+'%"></i><i class="sp" style="width:'+(p/t*100)+'%"></i></div>'+
        '<span class="dp">'+i+'/'+t+' &middot; '+Math.round(pct)+'%</span></div>'+
      '<table><thead><tr>'+
        '<th data-c="action">Action<span class="arr"></span></th>'+
        '<th data-c="type">Type<span class="arr"></span></th>'+
        '<th data-c="status">Status<span class="arr"></span></th>'+
        '<th data-c="ep">API Endpoint<span class="arr"></span></th>'+
        '<th>Assignee / PR</th></tr></thead><tbody>'+rows+'</tbody></table></div>';
  }
  list.innerHTML=html; dsel.innerHTML=opts;
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

document.getElementById('q').addEventListener('input',e=>{fQ=e.target.value.trim().toLowerCase();applyFilter();});
document.getElementById('f-domain').addEventListener('change',e=>{fDomain=e.target.value;applyFilter();});
document.querySelectorAll('#f-status button').forEach(b=>b.onclick=()=>{fStatus=b.dataset.v;document.querySelectorAll('#f-status button').forEach(x=>x.classList.remove('on'));b.classList.add('on');applyFilter();});
document.querySelectorAll('#f-type button').forEach(b=>b.onclick=()=>{fType=b.dataset.v;document.querySelectorAll('#f-type button').forEach(x=>x.classList.remove('on'));b.classList.add('on');applyFilter();});
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
</script>
</body>
</html>'''


def main():
    body = fetch_issue_body()
    domains = parse(body)
    states, merged, closed = reconcile(domains)
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
