"""The /calculator page (served by dashboard.py, auth-gated).

Two modes, one self-contained inline page (no build step, vanilla JS, same pattern as the dashboard):
  - "What-if": the standalone Black-Scholes premium-target calculator (modeled, ~8-12% richer than
    the live chain — noted in the UI).
  - "My accounts": reads /api/accounts + /api/account-options to suggest covered calls (above cost
    basis) on shares held and cash-secured puts on cash, across every Robinhood account. READ-ONLY /
    advisory — the bot trades only the ring-fenced Agentic account; the rest are placed manually.
"""

CALC_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Premium Calculator</title>
<style>
:root{--ground:#f5f6f8;--surface:#fff;--surface-2:#eef1f4;--line:#dfe4ea;--line2:#c9d0d9;
--text:#161c24;--muted:#5a6674;--accent:#9a6a12;--accent-soft:rgba(154,106,18,.12);
--pos:#1f9d63;--pos-soft:rgba(31,157,99,.10);--neg:#c1454e;--field:#fff;
--mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
--sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;}
@media(prefers-color-scheme:dark){:root{--ground:#0c1016;--surface:#141b24;--surface-2:#1a232e;
--line:#263140;--line2:#33404f;--text:#e7ecf2;--muted:#8b98a8;--accent:#e2b24c;
--accent-soft:rgba(226,178,76,.14);--pos:#4cc38a;--pos-soft:rgba(76,195,138,.13);--neg:#e06c75;--field:#0e141b;}}
*{box-sizing:border-box}body{margin:0;background:var(--ground);color:var(--text);font-family:var(--sans);font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:28px 20px 56px}
a{color:var(--accent)}
.top{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:10px}
h1{font-size:1.7rem;letter-spacing:-.02em;margin:0;font-weight:800}
.eyebrow{font-family:var(--mono);font-size:.66rem;letter-spacing:.2em;text-transform:uppercase;color:var(--accent);margin:0 0 8px}
.tabs{display:flex;gap:8px;margin:22px 0 26px}
.tab{font-family:var(--mono);font-size:.74rem;letter-spacing:.06em;text-transform:uppercase;padding:9px 15px;border:1px solid var(--line2);border-radius:7px;background:var(--surface);color:var(--muted);cursor:pointer}
.tab.on{border-color:var(--accent);color:var(--accent);background:var(--accent-soft)}
.tab:focus-visible{outline:2px solid var(--accent)}
.hide{display:none}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px 18px;margin-bottom:8px}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-family:var(--mono);font-size:.62rem;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
input,select{font-family:var(--mono);font-size:.96rem;color:var(--text);background:var(--field);border:1px solid var(--line2);border-radius:6px;padding:8px 10px;width:100%}
input:focus,select:focus{outline:2px solid var(--accent);border-color:var(--accent)}
.range-row{display:flex;align-items:center;gap:10px}input[type=range]{accent-color:var(--accent);flex:1}
.pair{display:flex;gap:8px;align-items:center}.pair span{color:var(--muted);font-family:var(--mono)}
.callout{background:var(--surface-2);border:1px solid var(--line);border-radius:9px;padding:18px 20px;margin:22px 0}
.callout .rec{font-size:1.08rem;margin:0;line-height:1.45}.callout .rec b{color:var(--accent)}
.callout.miss .rec b{color:var(--neg)}
.tblwrap{overflow-x:auto;border:1px solid var(--line);border-radius:9px;margin-top:12px;margin-bottom:6px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums}
th,td{text-align:right;padding:8px 13px;font-family:var(--mono);font-size:.82rem;white-space:nowrap;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:.6rem;letter-spacing:.1em;text-transform:uppercase;background:var(--surface-2)}
th:first-child,td:first-child{text-align:left}tbody tr:last-child td{border-bottom:0}
tr.hit td{background:var(--accent-soft)}
.chip{font-family:var(--mono);font-size:.56rem;letter-spacing:.06em;padding:2px 7px;border-radius:4px;text-transform:uppercase}
.chip.bot{background:var(--pos-soft);color:var(--pos)}.chip.adv{background:var(--accent-soft);color:var(--accent)}
h2{font-size:.98rem;margin:20px 0 2px}.sub{color:var(--muted);font-size:.85rem;margin:6px 0 12px}
.acctcard{border:1px solid var(--line);border-radius:9px;padding:16px 18px;margin-top:16px;background:var(--surface)}
.acctcard h3{margin:0 0 3px;font-size:1rem;display:flex;gap:9px;align-items:center;flex-wrap:wrap}
.acctcard .bal{font-family:var(--mono);color:var(--muted);font-size:.82rem;margin-bottom:2px}
.stat{border:1px solid var(--line);border-radius:7px;padding:11px 13px;background:var(--surface)}
.stat .k{font-family:var(--mono);font-size:.56rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.stat .v{font-family:var(--mono);font-size:1rem;margin-top:3px}
.totrow{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:18px 0}
.note{color:var(--muted);font-size:.78rem;margin-top:6px}
footer{margin-top:30px;padding-top:18px;border-top:1px solid var(--line);color:var(--muted);font-size:.76rem;line-height:1.6}
footer b{color:var(--text)}
@media(max-width:640px){.grid,.totrow{grid-template-columns:1fr 1fr}}
</style></head><body><div class="wrap">
<div class="top"><div><p class="eyebrow">Cash-secured put &middot; premium planner</p><h1>Premium Calculator</h1></div><a href="/dashboard">&larr; Dashboard</a></div>
<div class="tabs"><button class="tab on" id="tabWhatif" onclick="showTab('whatif')">CSP what-if</button><button class="tab" id="tabCC" onclick="showTab('cc')">Covered-call planner</button><button class="tab" id="tabAcct" onclick="showTab('acct')">My accounts</button></div>

<section id="whatif">
  <div class="grid">
    <div class="field"><label>Account value ($)</label><input type="number" id="acct" value="200000" step="1000"></div>
    <div class="field"><label>Weekly target (% of account)</label><div class="pair"><input type="number" id="tmin" value="1.0" step="0.1"><span>&ndash;</span><input type="number" id="tmax" value="2.0" step="0.1"></div></div>
    <div class="field"><label>This position allocation (%)</label><input type="number" id="alloc" value="30" step="1"></div>
    <div class="field"><label>Underlying price ($)</label><input type="number" id="spot" value="100" step="0.01"></div>
    <div class="field"><label>Implied volatility (%)</label><div class="range-row"><input type="range" id="iv" min="5" max="150" value="45"><input type="number" id="ivnum" value="45" style="width:64px"></div></div>
    <div class="field"><label>Days to expiration</label><input type="number" id="dte" value="10" step="1"></div>
  </div>
  <div class="callout" id="rec"><p class="rec" id="recText">&mdash;</p></div>
  <div class="tblwrap"><table><thead><tr><th>Strike</th><th>|&Delta;|</th><th>Premium</th><th>%/wk</th><th>Weekly $</th><th>Status</th></tr></thead><tbody id="wrows"></tbody></table></div>
  <p class="note" id="wnote"></p>
  <footer>Black&ndash;Scholes with a single IV (rate 4.5%). Modeled premiums run ~8&ndash;12% richer than the live chain &mdash; use this for the delta zone and confirm the exact premium on your chain. The <b>My accounts</b> tab uses real chain prices. Not investment advice.</footer>
</section>

<section id="ccplan" class="hide">
  <p class="sub" style="margin:14px 0 18px">Which covered call to sell so this asset pulls its <b>allocation-weighted share</b> of your weekly goal &mdash; while keeping call-away risk in check. The math collapses cleanly: every asset needs the same weekly yield on its value (your goal %), so higher-IV names hit it further out-of-the-money (safer), lower-IV names have to sell closer to the money (more call-away risk). Delta &asymp; the chance of being called away.</p>
  <div class="grid">
    <div class="field"><label>Account value ($)</label><input type="number" id="ccAcct" value="200000" step="1000"></div>
    <div class="field"><label>Weekly goal (% of account)</label><input type="number" id="ccGoal" value="1.0" step="0.1"></div>
    <div class="field"><label>This asset's allocation (%)</label><input type="number" id="ccAlloc" value="30" step="1"></div>
    <div class="field"><label>Share price ($)</label><input type="number" id="ccSpot" value="20" step="0.01"></div>
    <div class="field"><label>Implied volatility (%)</label><div class="range-row"><input type="range" id="ccIv" min="10" max="200" value="60"><input type="number" id="ccIvnum" value="60" style="width:64px"></div></div>
    <div class="field"><label>Days to expiration</label><input type="number" id="ccDte" value="10" step="1"></div>
    <div class="field" style="grid-column:1/-1"><label>Call-away tolerance (max delta &asymp; max % chance of assignment)</label><div class="range-row"><input type="range" id="ccMax" min="5" max="50" value="30"><span class="iv-val" id="ccMaxv" style="min-width:110px">0.30 &asymp; 30%</span></div></div>
  </div>
  <div class="callout" id="ccRec"><p class="rec" id="ccRecText">&mdash;</p></div>
  <div class="tblwrap"><table><thead><tr><th>Strike</th><th>|&Delta;| &asymp; call-away</th><th>Premium</th><th>%/wk</th><th>Weekly $</th><th>Status</th></tr></thead><tbody id="ccrows"></tbody></table></div>
  <p class="note" id="ccnote"></p>
  <footer>Modeled (Black&ndash;Scholes, single IV, rate 4.5%) so it works for any asset at any IV &mdash; premiums run a touch rich vs the live chain; confirm on your chain. Weekly yield is on the shares' current value. Delta approximates the probability the call finishes in-the-money (called away). Not investment advice.</footer>
</section>

<section id="acctpane" class="hide">
  <div class="field" style="max-width:360px"><label>Account</label><select id="acctSel" onchange="loadAcct()"></select></div>
  <div id="acctBody"><p class="sub" style="margin-top:16px">Loading your accounts&hellip;</p></div>
  <footer>Read-only advisory &mdash; suggestions use live option-chain prices and never place orders. The bot trades only your Agentic account; the rest are yours to place manually. Covered calls are shown out-of-the-money regardless of cost basis; a "below basis" tag marks strikes under your original cost (selling those collects premium and lowers your effective basis). Not investment advice.</footer>
</section>

</div>
<script>
var R=0.045;
function ncdf(x){var t=1/(1+0.2316419*Math.abs(x));var d=0.3989422804014327*Math.exp(-x*x/2);var p=d*t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))));return x>=0?1-p:p;}
function putP(S,K,T,s){if(T<=0||s<=0||S<=0||K<=0)return Math.max(0,K-S);var v=s*Math.sqrt(T),d1=(Math.log(S/K)+(R+s*s/2)*T)/v,d2=d1-v;return K*Math.exp(-R*T)*ncdf(-d2)-S*ncdf(-d1);}
function putD(S,K,T,s){if(T<=0||s<=0||S<=0||K<=0)return S<K?-1:0;var d1=(Math.log(S/K)+(R+s*s/2)*T)/(s*Math.sqrt(T));return ncdf(d1)-1;}
function tick(p){return p<15?0.5:p<50?1:p<100?2.5:p<250?5:10;}
function $(id){return document.getElementById(id);}
function nv(id){return parseFloat($(id).value);}
function d$(v){return "$"+Math.round(v||0).toLocaleString();}
function showTab(t){var P={whatif:"whatif",cc:"ccplan",acct:"acctpane"},B={whatif:"tabWhatif",cc:"tabCC",acct:"tabAcct"};for(var k in P){$(P[k]).classList.toggle("hide",k!==t);$(B[k]).classList.toggle("on",k===t);}if(t==="acct"&&!window._al){window._al=1;loadAccounts();}if(t==="cc")ccCalc();}
function calc(){var acct=nv("acct"),tmin=nv("tmin")/100,tmax=nv("tmax")/100,alloc=nv("alloc")/100,spot=nv("spot"),iv=nv("ivnum")/100,dte=Math.max(1,nv("dte")),T=dte/365,allocD=acct*alloc;
var tk=tick(spot),K=Math.floor(spot/tk)*tk;if(K>=spot)K-=tk;var rows=[],g=0;
while(g++<80&&K>0){var dd=Math.abs(putD(spot,K,T,iv));if(dd<0.045)break;if(dd<=0.46){var p=putP(spot,K,T,iv),wk=(p/K)*(7/dte);rows.push({K:K,dd:dd,p:p,wk:wk,ww:wk*allocD});}K-=tk;}rows.reverse();
var rec=null,i;for(i=0;i<rows.length;i++){if(rows[i].wk>=tmin){rec=rows[i];break;}}
var rc=$("rec"),rt=$("recText");
if(rec){rc.classList.remove("miss");rt.innerHTML="At <b>"+(iv*100).toFixed(0)+"% IV</b>, sell the <b>$"+rec.K.toFixed(2)+"</b> put ("+rec.dd.toFixed(2)+"&Delta;, ~$"+rec.p.toFixed(2)+") &rarr; <b>"+(rec.wk*100).toFixed(2)+"%/wk</b>, ~<b>"+d$(rec.ww)+"/wk</b> from this name &mdash; the safest strike still meeting your "+(tmin*100).toFixed(1)+"% floor.";}
else{rc.classList.add("miss");var b=null;for(i=0;i<rows.length;i++){if(!b||rows[i].wk>b.wk)b=rows[i];}rt.innerHTML=b?"<b>IV too low to hit target</b> at a safe delta &mdash; the richest in range ($"+b.K.toFixed(2)+", "+b.dd.toFixed(2)+"&Delta;) makes only "+(b.wk*100).toFixed(2)+"%/wk. Sell closer to the money, a shorter DTE, or accept less from this name.":"No strikes in range.";}
var tb=$("wrows");tb.innerHTML="";rows.forEach(function(r){var inb=r.wk>=tmin&&r.wk<=tmax;var st=inb?'<span class="chip" style="background:var(--accent-soft);color:var(--accent)">In target</span>':r.wk>tmax?'<span class="chip" style="color:var(--pos)">Hot</span>':'<span class="chip" style="color:var(--muted)">Low</span>';var tr=document.createElement("tr");if(inb)tr.className="hit";tr.innerHTML="<td>$"+r.K.toFixed(2)+"</td><td>"+r.dd.toFixed(2)+"</td><td>$"+r.p.toFixed(2)+"</td><td>"+(r.wk*100).toFixed(2)+"%</td><td>"+d$(r.ww)+"</td><td>"+st+"</td>";tb.appendChild(tr);});
$("wnote").innerHTML="<b>In target</b>=weekly yield in your "+(tmin*100).toFixed(2)+"&ndash;"+(tmax*100).toFixed(2)+"% band. <b>Hot</b>=more premium, closer to the money (more assignment risk). <b>Low</b>=safer, short of target.";}
["acct","tmin","tmax","alloc","spot","dte","ivnum"].forEach(function(id){$(id).addEventListener("input",calc);});
$("iv").addEventListener("input",function(){$("ivnum").value=$("iv").value;calc();});
$("ivnum").addEventListener("input",function(){$("iv").value=$("ivnum").value;});
calc();

function callP(S,K,T,s){if(T<=0||s<=0||S<=0||K<=0)return Math.max(0,S-K);var v=s*Math.sqrt(T),d1=(Math.log(S/K)+(R+s*s/2)*T)/v,d2=d1-v;return S*ncdf(d1)-K*Math.exp(-R*T)*ncdf(d2);}
function callD(S,K,T,s){if(T<=0||s<=0||S<=0||K<=0)return S>K?1:0;var d1=(Math.log(S/K)+(R+s*s/2)*T)/(s*Math.sqrt(T));return ncdf(d1);}
function ccCalc(){var acct=nv("ccAcct"),goal=nv("ccGoal")/100,alloc=nv("ccAlloc")/100,spot=nv("ccSpot"),iv=nv("ccIvnum")/100,dte=Math.max(1,nv("ccDte")),T=dte/365,maxD=nv("ccMax")/100;
var assetV=acct*alloc,targetWk=assetV*goal;
$("ccMaxv").innerHTML=maxD.toFixed(2)+" &asymp; "+(maxD*100).toFixed(0)+"%";
var tk=tick(spot),K=Math.ceil(spot/tk)*tk;if(K<=spot)K+=tk;var rows=[],g=0;
while(g++<80&&K<spot*3){var cd=callD(spot,K,T,iv);if(cd<0.03)break;if(cd<=0.55){var p=callP(spot,K,T,iv),wk=(p/spot)*(7/dte);rows.push({K:K,cd:cd,p:p,wk:wk,ww:wk*assetV});}K+=tk;}
var rec=null,i;for(i=0;i<rows.length;i++){if(rows[i].wk>=goal&&rows[i].cd<=maxD)rec=rows[i];} // lowest-delta (highest K) hitting goal within tolerance
var rc=$("ccRec"),rt=$("ccRecText");
var maxWk=rows.reduce(function(a,c){return c.wk>a?c.wk:a;},0);
if(rec){rc.classList.remove("miss");rt.innerHTML="This asset is <b>"+(alloc*100).toFixed(0)+"%</b> of the account, so its share of the weekly goal is ~<b>"+d$(targetWk)+"</b>. At <b>"+(iv*100).toFixed(0)+"% IV</b>, sell the <b>$"+rec.K.toFixed(2)+"</b> call ("+rec.cd.toFixed(2)+"&Delta; &asymp; <b>"+(rec.cd*100).toFixed(0)+"% call-away</b>, ~$"+rec.p.toFixed(2)+") &rarr; <b>"+(rec.wk*100).toFixed(2)+"%/wk</b>, ~"+d$(rec.ww)+"/wk. That's the safest (furthest-OTM) strike hitting your goal within your "+(maxD*100).toFixed(0)+"% call-away tolerance.";}
else{rc.classList.add("miss");var hits=rows.filter(function(r){return r.wk>=goal;});
if(hits.length){var safest=hits[hits.length-1];rt.innerHTML="To hit your goal you'd have to sell the <b>$"+safest.K.toFixed(2)+"</b> call at <b>"+safest.cd.toFixed(2)+"&Delta; ("+(safest.cd*100).toFixed(0)+"% call-away)</b> &mdash; above your "+(maxD*100).toFixed(0)+"% tolerance. Either raise your tolerance (accept more call-away risk), or accept a smaller contribution from this name.";}
else{rt.innerHTML="<b>IV too low to hit this asset's share safely.</b> Even close to the money the best is ~<b>"+(maxWk*100).toFixed(2)+"%/wk</b>, short of your "+(goal*100).toFixed(2)+"% goal. A lower-IV name like this should carry a <b>smaller</b> slice of the goal &mdash; or use a shorter DTE.";}}
var tb=$("ccrows");tb.innerHTML="";rows.forEach(function(r){var hit=r.wk>=goal&&r.cd<=maxD,risky=r.wk>=goal&&r.cd>maxD;var st=hit?'<span class="chip" style="background:var(--accent-soft);color:var(--accent)">In target</span>':risky?'<span class="chip" style="color:var(--neg)">Too risky</span>':'<span class="chip" style="color:var(--muted)">Low</span>';var tr=document.createElement("tr");if(hit)tr.className="hit";tr.innerHTML="<td>$"+r.K.toFixed(2)+"</td><td>"+r.cd.toFixed(2)+" &asymp; "+(r.cd*100).toFixed(0)+"%</td><td>$"+r.p.toFixed(2)+"</td><td>"+(r.wk*100).toFixed(2)+"%</td><td>"+d$(r.ww)+"</td><td>"+st+"</td>";tb.appendChild(tr);});
$("ccnote").innerHTML="<b>In target</b>=hits your "+(goal*100).toFixed(2)+"%/wk goal within your call-away tolerance. <b>Too risky</b>=hits the goal but delta is above your tolerance (likely called away). <b>Low</b>=safe delta but short of the goal.";}
["ccAcct","ccGoal","ccAlloc","ccSpot","ccDte","ccIvnum","ccMax"].forEach(function(id){$(id).addEventListener("input",ccCalc);});
$("ccIv").addEventListener("input",function(){$("ccIvnum").value=$("ccIv").value;ccCalc();});
$("ccIvnum").addEventListener("input",function(){$("ccIv").value=$("ccIvnum").value;});

function gj(u){return fetch(u,{credentials:"same-origin"}).then(function(r){if(!r.ok)throw new Error(u+" -> "+r.status);return r.json();});}
function loadAccounts(){gj("/api/accounts").then(function(d){window._am={};var sel=$("acctSel");sel.innerHTML="";var o=document.createElement("option");o.value="all";o.textContent="All accounts";sel.appendChild(o);(d.accounts||[]).forEach(function(a){window._am[a.account_number]=a;var op=document.createElement("option");op.value=a.account_number;op.textContent=(a.nickname||a.type||a.account_number)+" · "+d$(a.account_value)+(a.agentic?" (bot)":"");sel.appendChild(op);});loadAcct();}).catch(function(e){$("acctBody").innerHTML='<p class="sub">Could not load accounts: '+e.message+'</p>';});}
function tbl(title,rows,cols){if(!rows||!rows.length)return '<p class="sub">'+title+': none.</p>';var h='<h2>'+title+'</h2><div class="tblwrap"><table><thead><tr>'+cols.map(function(c){return '<th>'+c[0]+'</th>';}).join("")+'</tr></thead><tbody>';rows.forEach(function(r){h+='<tr>'+cols.map(function(c){return '<td>'+c[1](r)+'</td>';}).join("")+'</tr>';});return h+'</tbody></table></div>';}
function acctBlock(a){if(a.error)return '<div class="acctcard"><h3>'+a.account_number+'</h3><p class="sub">'+a.error+'</p></div>';
var m=(window._am||{})[a.account_number]||{};var title=m.nickname||m.type||a.account_number;var badge=m.agentic?'<span class="chip bot">Bot-traded</span>':'<span class="chip adv">Advisory &mdash; place manually</span>';
var h='<div class="acctcard"><h3>'+title+' '+badge+'</h3><div class="bal">Cash '+d$(a.buying_power)+' &middot; Value '+d$(a.account_value)+(a.weekly_target?' &middot; Weekly target '+d$(a.weekly_target):'')+'</div>';
if(a.holdings&&a.holdings.length)h+=tbl("Shares held",a.holdings,[["Symbol",function(r){return r.symbol;}],["Shares",function(r){return r.shares;}],["Cost basis",function(r){return "$"+r.cost_basis.toFixed(2);}],["Coverable",function(r){return r.coverable;}]]);
h+=tbl("Covered calls (OTM; premium lowers cost basis)",a.covered_calls,[["Name",function(r){return r.underlying;}],["Strike",function(r){return "$"+r.strike.toFixed(2)+(r.below_basis?' <span class="chip" style="color:var(--neg)">below basis</span>':'');}],["DTE",function(r){return r.dte;}],["Delta",function(r){return Math.abs(r.delta||0).toFixed(2);}],["Prem",function(r){return "$"+r.premium.toFixed(2);}],["Qty",function(r){return r.contracts;}],["%/wk",function(r){return (r.weekly_yield_pct||0).toFixed(2)+"%";}],["Weekly $",function(r){return d$(r.weekly_dollars);}]]);
h+=tbl("Cash-secured puts (on cash)",a.cash_secured_puts,[["Name",function(r){return r.underlying;}],["Strike",function(r){return "$"+r.strike.toFixed(2);}],["DTE",function(r){return r.dte;}],["Delta",function(r){return Math.abs(r.delta||0).toFixed(2);}],["Prem",function(r){return "$"+r.premium.toFixed(2);}],["Qty",function(r){return r.contracts;}],["Collateral",function(r){return d$(r.collateral);}],["%/wk",function(r){return (r.weekly_yield_pct||0).toFixed(2)+"%";}],["Weekly $",function(r){return d$(r.weekly_dollars);}]]);
return h+'</div>';}
function loadAcct(){var sel=$("acctSel");if(!sel.value)return;$("acctBody").innerHTML='<p class="sub" style="margin-top:16px">Pulling live chains&hellip;</p>';gj("/api/account-options?account="+encodeURIComponent(sel.value)).then(function(d){var h='';if(sel.value==="all"){var tot=(d.total_cc_weekly||0)+(d.total_csp_weekly||0);h+='<div class="totrow"><div class="stat"><div class="k">Covered-call weekly</div><div class="v">'+d$(d.total_cc_weekly)+'</div></div><div class="stat"><div class="k">CSP weekly (max)</div><div class="v">'+d$(d.total_csp_weekly)+'</div></div><div class="stat"><div class="k">Total vs target</div><div class="v">'+d$(tot)+' / '+d$(d.total_weekly_target)+'</div></div></div>';}
(d.accounts||[]).forEach(function(a){h+=acctBlock(a);});$("acctBody").innerHTML=h||'<p class="sub">No suggestions.</p>';}).catch(function(e){$("acctBody").innerHTML='<p class="sub">Error: '+e.message+'</p>';});}
</script></body></html>"""
