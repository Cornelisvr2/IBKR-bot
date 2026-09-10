"""
dashboard_server.py — Dagrapport-dashboard voor TTS, QFS, RVB en VDB

VERVANGT (9 sep 2026) het eerdere TTS-monitordashboard. UITGEBREID
(9 sep 2026) met VDB (VWAP Dynamic Bounce) als vierde strategie. Toont
per dag: tijdlijn van de handelsdag, scorekaarten per strategie, alle
trades met grafiek en detail, en de RVB/VDB-signalen die géén trade
werden. Doel: de Telegram-stroom terugbrengen tot alleen alarmen.

Bronnen (alleen lezen):
    logs/trade_journal.csv      -- afgeronde trades (journal_module.py)
    logs/charts/*.png           -- grafieken (chart_module.py)
    logs/rvb_signals.jsonl      -- RVB-signalen incl. overgeslagen (rvb_scan.py)
    logs/vdb_signals.jsonl      -- VDB-signalen incl. overgeslagen (vwap_bounce_scan.py)
    logs/vix_daily.jsonl        -- dagelijkse VIX-waarde (vix_daily_report.py)
    state.json                  -- per-strategie gesimuleerde saldi (state_module.py)

Uitsluitend Python-stdlib (http.server), geen extra dependencies.
Luistert ALLEEN op 127.0.0.1:8899 -- Caddy (zie Caddyfile.ibkr) zet er
HTTPS + basic-auth voor, exact zoals bij de HBAR-bot.

Routes:
    /                      dagrapport van vandaag
    /?date=2026-09-09      dagrapport van een andere dag
    /charts/<bestand>.png  grafiek
    /api/day?date=...      dezelfde data als JSON
    /health                voor monitoring

Starten: via systemd (ibkr-dashboard.service) of handmatig:
    cd /opt/strategy && python3 dashboard_server.py
"""

from __future__ import annotations

import csv
import html
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

STRATEGY_DIR = os.environ.get("STRATEGY_DIR", "/opt/strategy")
LOGS_DIR = os.path.join(STRATEGY_DIR, "logs")
JOURNAL_PATH = os.environ.get("TTS_JOURNAL_FILE", os.path.join(LOGS_DIR, "trade_journal.csv"))
CHARTS_DIR = os.path.join(LOGS_DIR, "charts")
SIGNALS_PATH = os.environ.get("RVB_SIGNAL_LOG", os.path.join(LOGS_DIR, "rvb_signals.jsonl"))
VDB_SIGNALS_PATH = os.environ.get("VDB_SIGNAL_LOG", os.path.join(LOGS_DIR, "vdb_signals.jsonl"))
VIX_REPORT_PATH = os.environ.get("VIX_REPORT_LOG", os.path.join(LOGS_DIR, "vix_daily.jsonl"))
STATE_PATH = os.environ.get("TTS_STATE_FILE", os.path.join(STRATEGY_DIR, "state.json"))
EVENTS_PATH = os.environ.get("EVENT_LOG_FILE", os.path.join(LOGS_DIR, "events.jsonl"))
HOST = os.environ.get("DASHBOARD_BIND", "127.0.0.1")   # 0.0.0.0 alleen in de Docker-Caddy-situatie, zie DASHBOARD_DEPLOY.md
PORT = int(os.environ.get("DASHBOARD_PORT", "8899"))

STRATEGIES = {
    "TTS": ("Touch & Turn", "eerste 90 min"),
    "QFS": ("Quick Flip", "eerste 90 min"),
    "RVB": ("Relative Volume Breakout", "hele dag"),
    "VDB": ("VWAP Dynamic Bounce", "hele dag"),
}
RESULT_LABELS = {
    "take_profit_hit": "Take-profit", "stop_loss_hit": "Stop-loss",
    "forced_close_90min": "Geforceerd (tijdslimiet)", "forced_close": "Geforceerd (sluiting)", "unknown": "Onbekend",
}
SESSION_START_MIN = 15 * 60 + 30   # 15:30 CEST
SESSION_END_MIN = 22 * 60          # 22:00 CEST
WEEKDAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MAANDEN = ["", "januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober", "november", "december"]


# ---------------------------------------------------------------------------
# Data laden
# ---------------------------------------------------------------------------

def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _local_time_from_utc(d: str, t: str) -> str:
    """Oude journal-rijen hebben alleen een UTC-tijd; zet om naar servertijd (CEST)."""
    try:
        utc = datetime.fromisoformat(f"{d}T{t}").replace(tzinfo=timezone.utc)
        return utc.astimezone().strftime("%H:%M")
    except ValueError:
        return t[:5]


def load_trades() -> list[dict]:
    if not os.path.exists(JOURNAL_PATH):
        return []
    trades = []
    with open(JOURNAL_PATH, newline="") as f:
        for r in csv.DictReader(f):
            strategy = (r.get("strategy") or "").strip()
            if not strategy:
                strategy = (r.get("oca_group") or "TTS_").split("_", 1)[0]
            if strategy not in STRATEGIES:
                strategy = "TTS"
            entry = _f(r.get("entry_price"), 0.0)
            sl = _f(r.get("stop_loss"), 0.0)
            qty = _f(r.get("quantity"), 0.0)
            pnl_net = _f(r.get("pnl_net"))
            pnl = pnl_net if pnl_net is not None else _f(r.get("pnl_estimate"), 0.0)
            risk = abs(entry - sl) * qty
            entry_time = (r.get("entry_time") or "")[:5] or _local_time_from_utc(r.get("date", ""), r.get("time", "00:00:00"))
            exit_time = (r.get("exit_time") or "")[:5] or _local_time_from_utc(r.get("date", ""), r.get("time", "00:00:00"))
            trades.append({
                "date": r.get("date", ""), "strategy": strategy, "symbol": r.get("symbol", ""),
                "direction": r.get("direction", ""), "entry_price": entry,
                "take_profit": _f(r.get("take_profit"), 0.0), "stop_loss": sl, "quantity": qty,
                "exit_price": _f(r.get("exit_price")), "result": r.get("result", "unknown"),
                "pnl": pnl, "pnl_gross": _f(r.get("pnl_estimate")), "fees": _f(r.get("fees")),
                "r_multiple": (pnl / risk) if risk > 0 else None, "risk": risk,
                "entry_time": entry_time, "exit_time": exit_time,
                "oca_group": r.get("oca_group", ""), "chart": r.get("chart", ""),
                "note": r.get("pnl_note", ""),
            })
    return trades


def load_signals(day: str) -> list[dict]:
    """
    Signalen-zonder-trade van zowel RVB als VDB (elk zijn eigen
    logbestand, bewust niet samengevoegd tot één bestand -- elke
    scanner schrijft alleen zijn eigen log). `strategy` wordt gezet op
    "RVB" als het veld ontbreekt (oudere regels, vóór VDB bestond).
    """
    out = []
    for path, default_strategy in ((SIGNALS_PATH, "RVB"), (VDB_SIGNALS_PATH, "VDB")):
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                try:
                    s = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if s.get("time", "").startswith(day):
                    s.setdefault("strategy", default_strategy)
                    out.append(s)
    return sorted(out, key=lambda s: s.get("time", ""))


def load_events(day: str) -> list[dict]:
    """Meldingen van één dag uit events.jsonl (telegram_notify.log_event)."""
    if not os.path.exists(EVENTS_PATH):
        return []
    out = []
    with open(EVENTS_PATH) as f:
        for line in f:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("time", "").startswith(day):
                out.append(ev)
    return out


def load_vix_report(day: str) -> dict | None:
    """
    Laatst bekende VIX-rapport van de gegeven dag (er kan er maar één
    per dag zijn, via cron -- maar als het script ooit dubbel draait,
    pakken we bewust de LAATSTE regel).
    """
    if not os.path.exists(VIX_REPORT_PATH):
        return None
    laatste = None
    with open(VIX_REPORT_PATH) as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("time", "").startswith(day):
                laatste = r
    return laatste


def load_strategy_balances() -> dict:
    """
    Per-strategie compounding-saldi (9 sep 2026) -- elke strategie in
    STRATEGIES heeft sinds de A/B/C/D-fairness-fix zijn EIGEN,
    onafhankelijke saldo (state_module.get_strategy_balance), i.p.v.
    één gedeeld "simulated_balance"-veld (dat nu uitsluitend nog voor
    VIX Rider bestaat, welke niet op dit dashboard staat).
    """
    try:
        with open(STATE_PATH) as f:
            balances = json.load(f).get("strategy_balances", {})
    except Exception:
        balances = {}
    return {code: balances.get(code, 2000.0) for code in STRATEGIES}


# ---------------------------------------------------------------------------
# Samenvatten
# ---------------------------------------------------------------------------

def load_signals_range(code: str, tot_en_met: str, dagen: int = 20) -> list[dict]:
    """Alle signalen van één strategie in de `dagen` handelsdagen t/m `tot_en_met`."""
    path = SIGNALS_PATH if code == "RVB" else VDB_SIGNALS_PATH
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            try:
                x = json.loads(line)
            except json.JSONDecodeError:
                continue
            if x.get("time", "")[:10] <= tot_en_met:
                out.append(x)
    dagen_set = sorted({x["time"][:10] for x in out})[-dagen:]
    return [x for x in out if x["time"][:10] in dagen_set]


def paper_summary(sigs: list[dict]) -> dict:
    n = len(sigs)
    wins = sum(1 for x in sigs if (x.get("paper_pnl") or 0) > 0)
    pnl = sum(x.get("paper_pnl") or 0 for x in sigs)
    rs = [x["paper_r"] for x in sigs if x.get("paper_r") is not None]
    return {"n": n, "wins": wins, "pnl": round(pnl, 2), "winrate": round(100 * wins / n) if n else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None}


def summarize(trades: list[dict]) -> dict:
    n = len(trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    pnl = sum(t["pnl"] for t in trades)
    rs = [t["r_multiple"] for t in trades if t["r_multiple"] is not None]
    return {"trades": n, "wins": wins, "losses": n - wins, "pnl": round(pnl, 2),
            "winrate": round(100 * wins / n) if n else None,
            "avg_r": round(sum(rs) / len(rs), 2) if rs else None}


def _leid_strategie_af(tekst: str, sym_strat: dict) -> str:
    t = tekst.lstrip("✅🛑⏰⏱️⚠️🚨📤🧪ℹ️🔍👀🎯⏭️🏁 ")
    for naam in ("VDB", "RVB", "QFS", "TTS"):
        if f" {naam} " in f" {t[:40]} " or f"[DRY-RUN] {naam}" in t:
            return naam
    if "omkeerpatroon" in t or "Quick Flip" in t:
        return "QFS"
    if "Touch & Turn" in t or "VIX" in t:
        return "TTS+QFS" if "Touch & Turn" in t else ""
    sym = t.split(" ", 1)[0].rstrip(":")
    return sym_strat.get(sym, "")


def build_day(day: date, all_trades: list[dict]) -> dict:
    iso = day.isoformat()
    monday = day - timedelta(days=day.weekday())
    day_trades = [t for t in all_trades if t["date"] == iso]
    week_trades = [t for t in all_trades if monday.isoformat() <= t["date"] <= iso]
    trading_days = sorted({t["date"] for t in all_trades if t["date"] <= iso})[-20:]
    d20_trades = [t for t in all_trades if t["date"] in trading_days]

    per_strategy = {}
    balances = load_strategy_balances()
    for code in STRATEGIES:
        mine = lambda ts: [t for t in ts if t["strategy"] == code]
        first = min((t["date"] for t in all_trades if t["strategy"] == code), default=None)
        per_strategy[code] = {
            "day": summarize(mine(day_trades)), "week": summarize(mine(week_trades)),
            "d20": summarize(mine(d20_trades)), "first_date": first, "balance": balances[code],
        }

    signals = load_signals(iso)
    events = load_events(iso)
    # Strategielabel afleiden voor regels van vóór 9 sep 2026 (toen nog
    # zonder `strategy`-veld): eerst uit de tekst, anders uit de journal
    # (welke strategie handelde dit symbool op deze dag).
    sym_strat = {t["symbol"]: t.get("strategy", "") for t in day_trades if t.get("strategy")}
    for ev in events:
        if not ev.get("strategy"):
            ev["strategy"] = _leid_strategie_af(ev.get("text", ""), sym_strat)
    # PAPIEREN metrics (9 sep 2026): dry-run-signalen van RVB/VDB die door
    # papier_uitkomsten.py zijn nagespeeld -- vandaag en laatste 20 dagen.
    paper = {}
    for code in ("RVB", "VDB"):
        alle = [x for x in load_signals_range(code, iso) if x.get("paper_result")]
        paper[code] = {"day": paper_summary([x for x in alle if x["time"].startswith(iso)]),
                       "d20": paper_summary(alle)}
    dates = sorted({t["date"] for t in all_trades})
    prev_days = [d for d in dates if d < iso]
    next_days = [d for d in dates if d > iso]
    return {
        "date": iso, "trades": sorted(day_trades, key=lambda t: t["entry_time"]),
        "total": summarize(day_trades), "week": summarize(week_trades),
        "per_strategy": per_strategy, "balance": round(sum(balances.values()), 2),
        "signals_skipped": [s for s in signals if s.get("status") == "skipped"],
        "signals_dryrun": [s for s in signals if s.get("status") == "dry-run"],
        "paper": paper,
        "vix": load_vix_report(iso),
        "events": events,
        "event_counts": {lvl: sum(1 for e in events if e.get("level") == lvl) for lvl in ("urgent", "warning", "info", "decision")},
        "prev_date": prev_days[-1] if prev_days else None, "next_date": next_days[0] if next_days else None,
        "generated_at": datetime.now().strftime("%H:%M"),
    }


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

CSS = """
:root{--paper:#eef1f4;--sheet:#fff;--ink:#18232e;--ink-2:#4c5a68;--ink-3:#8a97a4;--rule:#d6dce3;--tts:#2457a6;--qfs:#7a3e9d;--rvb:#0f8a78;--vdb:#b8720a;--win:#1e8e5a;--loss:#c0392b;--flat:#8a97a4}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font-family:"Segoe UI",-apple-system,"Helvetica Neue",Arial,sans-serif;font-size:15px;line-height:1.45;font-variant-numeric:tabular-nums}
.page{max-width:1040px;margin:0 auto;padding:28px 20px 60px}
header{display:flex;align-items:baseline;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:8px}header h1{font-size:26px;font-weight:600;margin:0;letter-spacing:-.01em}
.daynav{display:flex;gap:6px}.daynav a,.daynav span{border:1px solid var(--rule);background:var(--sheet);border-radius:6px;padding:5px 12px;color:var(--ink-2);text-decoration:none}.daynav span{opacity:.45}.daynav a:hover,.daynav a:focus-visible{border-color:var(--ink-2);color:var(--ink);outline:none}
.daytotal{font-size:15px;color:var(--ink-2);margin:0 0 24px}.daytotal strong{font-size:20px;font-weight:600;margin-right:6px}
.pos{color:var(--win)}.neg{color:var(--loss)}.zero{color:var(--flat)}
.timeline{background:var(--sheet);border:1px solid var(--rule);border-radius:10px;padding:18px 20px 12px;margin-bottom:20px}.timeline h2{font-size:14px;font-weight:600;color:var(--ink-2);margin:0 0 12px}.timeline svg{width:100%;height:auto;display:block}.timeline text{font-size:11px;fill:var(--ink-3)}.tick{stroke:var(--rule)}
.vix-badge{font-weight:400;color:var(--ink-3);font-size:12px}
.bar{rx:3}.bar.TTS{fill:var(--tts)}.bar.QFS{fill:var(--qfs)}.bar.RVB{fill:var(--rvb)}.bar.VDB{fill:var(--vdb)}.bar.loss{opacity:.45}
.legend{display:flex;gap:18px;font-size:12px;color:var(--ink-2);margin-top:6px;flex-wrap:wrap}.legend span::before{content:"";display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px;vertical-align:-1px}.legend .TTS::before{background:var(--tts)}.legend .QFS::before{background:var(--qfs)}.legend .RVB::before{background:var(--rvb)}.legend .VDB::before{background:var(--vdb)}.legend .dim::before{background:var(--ink-3);opacity:.45}
.scores{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:24px}.score{background:var(--sheet);border:1px solid var(--rule);border-left-width:5px;border-radius:10px;padding:14px 16px}.score.TTS{border-left-color:var(--tts)}.score.QFS{border-left-color:var(--qfs)}.score.RVB{border-left-color:var(--rvb)}.score.VDB{border-left-color:var(--vdb)}
.score h3{margin:0;font-size:15px;font-weight:600}.score .sub{color:var(--ink-3);font-size:12px;margin-bottom:10px}.score .pnl{font-size:24px;font-weight:600;line-height:1.1}.score dl{display:grid;grid-template-columns:auto 1fr;gap:2px 12px;margin:10px 0 0;font-size:13px}.score dt{color:var(--ink-3)}.score dd{margin:0;text-align:right}.score .wtd{border-top:1px solid var(--rule);margin-top:10px;padding-top:8px;font-size:12px;color:var(--ink-2)}
.trades h2{font-size:16px;font-weight:600;margin:0 0 10px;display:flex;justify-content:space-between;align-items:baseline}.filters{display:flex;gap:6px}.filters button{border:1px solid var(--rule);background:var(--sheet);border-radius:999px;padding:3px 11px;font:inherit;font-size:12px;cursor:pointer;color:var(--ink-2)}.filters button[aria-pressed=true]{background:var(--ink);color:#fff;border-color:var(--ink)}
.trade{background:var(--sheet);border:1px solid var(--rule);border-radius:10px;margin-bottom:8px;overflow:hidden}.trade summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:8px 52px 44px 70px 1fr 150px 90px 24px;align-items:center;gap:12px;padding:10px 14px 10px 0}.trade summary::-webkit-details-marker{display:none}.trade summary:focus-visible{outline:2px solid var(--ink-2);outline-offset:-2px}
.swatch{align-self:stretch}.trade.TTS .swatch{background:var(--tts)}.trade.QFS .swatch{background:var(--qfs)}.trade.RVB .swatch{background:var(--rvb)}.trade.VDB .swatch{background:var(--vdb)}
.t-time{color:var(--ink-2);font-size:13px}.t-strat{font-size:12px;font-weight:600;letter-spacing:.02em}.trade.TTS .t-strat{color:var(--tts)}.trade.QFS .t-strat{color:var(--qfs)}.trade.RVB .t-strat{color:var(--rvb)}.trade.VDB .t-strat{color:var(--vdb)}.t-sym{font-weight:600}.t-desc{color:var(--ink-2);font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.t-result{font-size:13px}.t-pnl{text-align:right;font-weight:600}.chev{color:var(--ink-3);transition:transform .15s}.trade[open] .chev{transform:rotate(90deg)}
.detail{display:grid;grid-template-columns:1fr 260px;gap:18px;padding:4px 14px 16px 20px;border-top:1px solid var(--rule)}.detail figure{margin:0}.detail img{width:100%;height:auto;border:1px solid var(--rule);border-radius:6px;background:#fbfcfd}.detail .nochart{border:1px dashed var(--rule);border-radius:6px;padding:40px;text-align:center;color:var(--ink-3);font-size:13px}.detail dl{display:grid;grid-template-columns:auto 1fr;gap:3px 14px;font-size:13px;margin:8px 0 0}.detail dt{color:var(--ink-3)}.detail dd{margin:0}.detail .note{font-size:13px;color:var(--ink-2);margin-top:12px;padding-top:10px;border-top:1px solid var(--rule)}
.empty{background:var(--sheet);border:1px dashed var(--rule);border-radius:10px;padding:26px;color:var(--ink-3);text-align:center}
.signals{margin-top:26px;font-size:13px;color:var(--ink-2)}.signals h2{font-size:14px;font-weight:600;margin:0 0 6px;color:var(--ink-2)}.signals ul{margin:0;padding-left:18px}
.events{margin-top:26px}.events h2{font-size:16px;font-weight:600;margin:0 0 10px;display:flex;justify-content:space-between;align-items:baseline}.events .counts{font-size:12px;color:var(--ink-3);font-weight:400}
.events ol{list-style:none;margin:0;padding:0;background:var(--sheet);border:1px solid var(--rule);border-radius:10px;overflow:hidden}.events li{display:grid;grid-template-columns:52px 74px 1fr 70px;gap:12px;padding:8px 14px;border-top:1px solid var(--rule);font-size:13px;align-items:baseline}.events li:first-child{border-top:0}
.events .lvl{font-weight:600;font-size:12px}.events li.urgent .lvl{color:var(--loss)}.events li.warning .lvl{color:#b7791f}.events li.info .lvl{color:var(--ink-3)}.events li.urgent{background:#fdf3f2}.events .txt{white-space:pre-wrap;color:var(--ink-2)}.events li.urgent .txt{color:var(--ink)}.events .tg{font-size:12px;color:var(--ink-3);text-align:right}
.papier{margin-top:8px;padding-top:8px;border-top:1px dashed var(--rule);font-size:12px;color:var(--ink-2)}.muted{color:var(--ink-3)}.events li.hidden{display:none}.events li{grid-template-columns:52px 62px 48px 1fr 70px}.events .strat{font-size:11px;font-weight:600;color:var(--ink-3);letter-spacing:.02em}.events li.decision .lvl{color:#2b6cb0}.events li.decision .txt{color:var(--ink-2)}.ev-filters button{font:inherit;font-size:12px;padding:2px 8px;border:1px solid var(--rule);border-radius:999px;background:var(--sheet);cursor:pointer;margin-left:4px}.ev-filters button[aria-pressed="true"]{background:var(--ink);color:#fff;border-color:var(--ink)}
.footer{margin-top:30px;font-size:12px;color:var(--ink-3)}
@media(max-width:720px){.scores{grid-template-columns:1fr}.events li{grid-template-columns:44px 56px 40px 1fr}.events .tg{display:none}.trade summary{grid-template-columns:8px 46px 40px 56px 1fr 70px 20px}.t-result{display:none}.detail{grid-template-columns:1fr}}
@media(prefers-reduced-motion:reduce){.chev{transition:none}}
"""

JS = """
document.querySelectorAll('.filters:not(.ev-filters) button').forEach(b=>b.addEventListener('click',()=>{
  document.querySelectorAll('.filters:not(.ev-filters) button').forEach(x=>x.setAttribute('aria-pressed',x===b));
  const f=b.dataset.f;document.querySelectorAll('.trade').forEach(t=>{t.style.display=(f==='all'||t.classList.contains(f))?'':'none';});
}));
const evState={s:'',dec:true};function applyEv(){document.querySelectorAll('.events li').forEach(l=>{const okS=!evState.s||l.dataset.strategy===evState.s;const okD=evState.dec||!l.classList.contains('decision');l.classList.toggle('hidden',!(okS&&okD));});}
document.querySelectorAll('.ev-filters .ev-f').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('.ev-filters .ev-f').forEach(x=>x.setAttribute('aria-pressed',x===b));evState.s=b.dataset.s;applyEv();}));
const togD=document.getElementById('toggle-decisions');if(togD){togD.addEventListener('click',()=>{evState.dec=togD.getAttribute('aria-pressed')!=='true';togD.setAttribute('aria-pressed',evState.dec);applyEv();});}
"""


def eur(x: float | None, sign: bool = True) -> str:
    if x is None:
        return "–"
    s = f"{x:+,.2f}" if sign else f"{x:,.2f}"
    return "€" + s.replace(",", "X").replace(".", ",").replace("X", ".")


def cls(x: float | None) -> str:
    return "zero" if not x else ("pos" if x > 0 else "neg")


def _min(hhmm: str) -> int | None:
    try:
        h, m = hhmm.split(":")[:2]
        return int(h) * 60 + int(m)
    except ValueError:
        return None


def timeline_svg(trades: list[dict]) -> str:
    x0, x1, y0 = 40, 1000, 80
    px_per_min = (x1 - x0) / (SESSION_END_MIN - SESSION_START_MIN)
    max_abs = max((abs(t["pnl"]) for t in trades), default=1) or 1
    parts = ['<svg viewBox="0 0 1000 150" role="img" aria-label="Trades uitgezet in de tijd, hoogte is resultaat">']
    for h in range(SESSION_START_MIN, SESSION_END_MIN + 1, 60):
        x = x0 + (h - SESSION_START_MIN) * px_per_min
        parts.append(f'<line class="tick" x1="{x:.1f}" y1="20" x2="{x:.1f}" y2="118"/><text x="{x:.1f}" y="135">{h // 60:02d}:{h % 60:02d}</text>')
    parts.append(f'<rect x="{x0}" y="20" width="{60 * px_per_min:.1f}" height="98" fill="#0f8a78" opacity=".06"/><text x="{x0 + 6}" y="32" fill="#0f8a78">ORB</text>')
    parts.append(f'<rect x="{x0}" y="20" width="{90 * px_per_min:.1f}" height="98" fill="#2457a6" opacity=".05"/><text x="{x0 + 60 * px_per_min + 8:.1f}" y="32" fill="#2457a6">90-min TTS/QFS</text>')
    parts.append(f'<line x1="{x0}" y1="{y0}" x2="{x1}" y2="{y0}" stroke="#8a97a4"/>')
    for t in trades:
        a, b = _min(t["entry_time"]), _min(t["exit_time"])
        if a is None:
            continue
        b = b if b is not None and b > a else a + 5
        x = x0 + (a - SESSION_START_MIN) * px_per_min
        w = max(6.0, (b - a) * px_per_min)
        hgt = max(4.0, 56 * abs(t["pnl"]) / max_abs)
        y = y0 - hgt if t["pnl"] >= 0 else y0
        loss = " loss" if t["pnl"] < 0 else ""
        title = html.escape(f"{t['strategy']} {t['symbol']} {t['direction']} {t['entry_time']}–{t['exit_time']} {eur(t['pnl'])}")
        parts.append(f'<rect class="bar {t["strategy"]}{loss}" x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{hgt:.1f}"><title>{title}</title></rect>')
    parts.append("</svg>")
    return "".join(parts)


def trade_html(t: dict) -> str:
    e = html.escape
    res = RESULT_LABELS.get(t["result"], t["result"])
    desc = f"{t['direction']} {t['quantity']:g} × {eur(t['entry_price'], sign=False)} · TP {t['take_profit']:.2f} / SL {t['stop_loss']:.2f}"
    chart = (f'<img src="/charts/{e(t["chart"])}" alt="Grafiek {e(t["symbol"])}" loading="lazy">' if t["chart"]
             else '<div class="nochart">Geen grafiek voor deze trade</div>')
    r = f"{t['r_multiple']:+.1f} R" if t["r_multiple"] is not None else "–"
    fees = f"{eur(t['pnl_gross'])} / {eur(-t['fees']) if t['fees'] is not None else '–'}"
    exit_p = f"{t['exit_time']} @ {t['exit_price']:.2f}" if t["exit_price"] is not None else t["exit_time"]
    return f"""<details class="trade {t['strategy']}">
<summary><span class="swatch"></span><span class="t-time">{e(t['entry_time'])}</span><span class="t-strat">{t['strategy']}</span><span class="t-sym">{e(t['symbol'])}</span>
<span class="t-desc">{e(desc)}</span><span class="t-result {cls(t['pnl'])}">{e(res)} {e(t['exit_time'])}</span><span class="t-pnl {cls(t['pnl'])}">{eur(t['pnl'])}</span><span class="chev">›</span></summary>
<div class="detail"><figure>{chart}</figure><div><dl>
<dt>Entry</dt><dd>{e(t['entry_time'])} @ {t['entry_price']:.2f}</dd><dt>Exit</dt><dd>{e(exit_p)}</dd>
<dt>Risico</dt><dd>{eur(t['risk'], sign=False)} · {r}</dd><dt>Bruto / fees</dt><dd>{fees}</dd><dt>OCA</dt><dd>{e(t['oca_group']) or '–'}</dd></dl>
{f'<p class="note">{e(t["note"])}</p>' if t['note'] else ''}</div></div></details>"""


def papier_html(d: dict, code: str) -> str:
    p = d.get("paper", {}).get(code)
    if not p or not p["d20"]["n"]:
        return ""
    dd, d20 = p["day"], p["d20"]
    vandaag = (f'{eur(dd["pnl"])} over {dd["n"]} signalen ({dd["wins"]} winst)' if dd["n"] else "geen signalen")
    wr = f'{d20["winrate"]} % ({d20["n"]})' if d20["winrate"] is not None else "–"
    avg_r = f'{d20["avg_r"]:+.1f} R' if d20["avg_r"] is not None else "–"
    return (f'<div class="papier"><strong>Papier (dry-run)</strong> vandaag {vandaag} · '
            f'20d {eur(d20["pnl"])} · winrate {wr} · gem. {avg_r}</div>')


def render(d: dict) -> str:
    e = html.escape
    day = date.fromisoformat(d["date"])
    titel = f"{WEEKDAGEN[day.weekday()].capitalize()} {day.day} {MAANDEN[day.month]} {day.year}"
    tot, wk = d["total"], d["week"]
    nav_prev = f'<a href="/?date={d["prev_date"]}">← {d["prev_date"][5:]}</a>' if d["prev_date"] else '<span>← eerder</span>'
    nav_next = f'<a href="/?date={d["next_date"]}">{d["next_date"][5:]} →</a>' if d["next_date"] else '<span>later →</span>'
    saldo = f" · totaal saldo {eur(d['balance'], sign=False)} (4x €2.000 basis)" if d["balance"] is not None else ""
    v = d.get("vix")
    vix_badge = (f' <span class="vix-badge">VIX {v["vix"]:.2f} · TTS/QFS {v["scalper_pct"]*100:.0f}% '
                f'· VIX Rider {v["macro_panic_pct"]*100:.0f}%</span>') if v else ""
    daytotal = (f'<strong class="{cls(tot["pnl"])}">{eur(tot["pnl"])}</strong> netto over {tot["trades"]} trades '
                f'({tot["wins"]} winst, {tot["losses"]} verlies){saldo} · week {eur(wk["pnl"])}') if tot["trades"] else "Geen afgeronde trades op deze dag."

    cards = []
    for code, (naam, venster) in STRATEGIES.items():
        s = d["per_strategy"][code]
        dd, ww, d20 = s["day"], s["week"], s["d20"]
        wr = f"{d20['winrate']} % ({d20['trades']} trades)" if d20["winrate"] is not None else "–"
        avg_r = f"{dd['avg_r']:+.1f} R" if dd["avg_r"] is not None else "–"
        sinds = f" · sinds {s['first_date']}" if s["first_date"] else ""
        cards.append(f"""<article class="score {code}"><h3>{e(naam)}</h3><div class="sub">{code} · {venster}</div>
<div class="pnl {cls(dd['pnl'])}">{eur(dd['pnl'])}</div>
<dl><dt>Trades</dt><dd>{dd['trades']} ({dd['wins']} winst)</dd><dt>Gem. R</dt><dd>{avg_r}</dd><dt>Winrate 20d</dt><dd>{wr}</dd><dt>Saldo</dt><dd>{eur(s['balance'], sign=False)}</dd></dl>
<div class="wtd">Deze week {eur(ww['pnl'])} · 20 dagen {eur(d20['pnl'])}{sinds}</div>{papier_html(d, code)}</article>""")

    trades = "".join(trade_html(t) for t in d["trades"]) or '<div class="empty">Geen trades. Zodra een trade sluit verschijnt hij hier met grafiek.</div>'
    sig_items = "".join(
        f"<li>{e(s.get('time', '')[11:16])} <strong>{e(s.get('strategy', ''))}</strong> {e(s.get('symbol', ''))} "
        f"{e(s.get('direction', ''))} — {e(s.get('reason', 'overgeslagen'))}</li>"
        for s in d["signals_skipped"])
    signals = f'<section class="signals"><h2>Signalen zonder trade ({len(d["signals_skipped"])})</h2><ul>{sig_items}</ul></section>' if sig_items else ""

    def _dry(x):
        basis = (f"<li>{e(x.get('time', '')[11:16])} <strong>{e(x.get('strategy', ''))}</strong> {e(x.get('symbol', ''))} "
                 f"{e(x.get('direction', ''))} @ {float(x.get('entry', 0)):.2f}")
        if x.get("take_profit"):
            basis += f" · TP {float(x['take_profit']):.2f} / SL {float(x['stop_loss']):.2f}"
        if x.get("paper_result"):
            pnl = x.get("paper_pnl") or 0
            basis += (f' — <span class="{cls(pnl)}">{e(RESULT_LABELS.get(x["paper_result"], x["paper_result"]))} '
                      f'@ {float(x["paper_exit"]):.2f} ({e(x.get("paper_exit_time", "")[11:16])}) {eur(pnl)}</span>')
        else:
            basis += ' — <span class="muted">uitkomst volgt na sluiting</span>'
        return basis + "</li>"
    dry_items = "".join(_dry(x) for x in d.get("signals_dryrun", []))
    signals = (f'<section class="signals"><h2>Dry-run-signalen ({len(d["signals_dryrun"])}) · papieren uitkomst</h2><ul>{dry_items}</ul></section>'
               if dry_items else "") + signals

    LVL = {"urgent": "Urgent", "warning": "Let op", "info": "Info", "decision": "Besluit"}
    # CHRONOLOGISCH (9 sep 2026, op verzoek): het logboek leest als het
    # verhaal van de dag -- alle regels zichtbaar, incl. beslissingen.
    # (Voorheen urgent-eerst en info standaard verborgen, waardoor een
    # rustige dag ten onrechte leeg oogde.)
    evs = sorted(d["events"], key=lambda ev: ev.get("time", ""))
    strategieen_in_log = sorted({ev.get("strategy", "") for ev in evs if ev.get("strategy")})
    ev_items = "".join(
        f'<li class="{e(ev.get("level", "info"))}" data-strategy="{e(ev.get("strategy", ""))}"><span class="t-time">{e(ev.get("time", "")[11:16])}</span>'
        f'<span class="lvl">{LVL.get(ev.get("level"), "Info")}</span>'
        f'<span class="strat">{e(ev.get("strategy", ""))}</span>'
        f'<span class="txt">{e(ev.get("text", ""))}</span>'
        f'<span class="tg">{"Telegram" if ev.get("telegram") else ""}</span></li>'
        for ev in evs)
    c = d["event_counts"]
    strat_knoppen = "".join(f'<button type="button" class="ev-f" data-s="{e(st)}" aria-pressed="false">{e(st)}</button>' for st in strategieen_in_log)
    events_html = (f'<section class="events"><h2>Logboek van de dag <span class="counts">{c["urgent"]} urgent · {c["warning"]} let op · {c["info"]} info · {c["decision"]} besluiten</span>'
                   f'<div class="filters ev-filters"><button type="button" class="ev-f" data-s="" aria-pressed="true">Alles</button>{strat_knoppen}'
                   f'<button type="button" id="toggle-decisions" aria-pressed="true">Besluiten</button></div></h2><ol>{ev_items}</ol></section>'
                   if evs else '<section class="events"><h2>Logboek van de dag</h2><div class="empty">Geen meldingen of beslissingen op deze dag.</div></section>')

    return f"""<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dagrapport — {e(titel)}</title><style>{CSS}</style></head><body><div class="page">
<header><h1>{e(titel)}</h1><nav class="daynav" aria-label="Dag kiezen">{nav_prev}{nav_next}</nav></header>
<p class="daytotal">{daytotal}</p>
<section class="timeline" aria-label="Verloop van de handelsdag"><h2>Handelsdag 15:30–22:00 CEST{vix_badge}</h2>{timeline_svg(d['trades'])}
<div class="legend"><span class="TTS">Touch &amp; Turn</span><span class="QFS">Quick Flip</span><span class="RVB">Relative Volume Breakout</span><span class="VDB">VWAP Dynamic Bounce</span><span class="dim">Verliestrade (transparant, onder de lijn)</span></div></section>
<section class="scores" aria-label="Resultaat per strategie">{''.join(cards)}</section>
<section class="trades"><h2>Trades van deze dag<div class="filters" role="group" aria-label="Filter op strategie">
<button type="button" aria-pressed="true" data-f="all">Alle</button><button type="button" aria-pressed="false" data-f="TTS">TTS</button><button type="button" aria-pressed="false" data-f="QFS">QFS</button><button type="button" aria-pressed="false" data-f="RVB">RVB</button><button type="button" aria-pressed="false" data-f="VDB">VDB</button></div></h2>
{trades}</section>{signals}{events_html}
<p class="footer">Bron: trade_journal.csv, events.jsonl, rvb_signals.jsonl, vdb_signals.jsonl en logs/charts. Pagina gegenereerd {d['generated_at']}.</p>
</div><script>{JS}</script></body></html>"""


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/health":
            return self._send(200, b"ok", "text/plain")
        if url.path.startswith("/charts/"):
            return self._send_chart(url.path[len("/charts/"):])
        try:
            day = date.fromisoformat(q.get("date", [date.today().isoformat()])[0])
        except ValueError:
            return self._send(400, b"ongeldige datum", "text/plain")
        data = build_day(day, load_trades())
        if url.path == "/api/day":
            return self._send(200, json.dumps(data, default=str).encode(), "application/json")
        if url.path == "/":
            return self._send(200, render(data).encode(), "text/html; charset=utf-8")
        self._send(404, b"niet gevonden", "text/plain")

    def _send_chart(self, name: str):
        safe = os.path.basename(name)
        pad = os.path.join(CHARTS_DIR, safe)
        if not safe.endswith(".png") or not os.path.isfile(pad):
            return self._send(404, b"geen grafiek", "text/plain")
        with open(pad, "rb") as f:
            self._send(200, f.read(), "image/png", cache="max-age=86400")

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {self.address_string()} {fmt % args}\n")


if __name__ == "__main__":
    print(f"Dashboard op http://{HOST}:{PORT} (journal: {JOURNAL_PATH})")
    HTTPServer((HOST, PORT), Handler).serve_forever()
