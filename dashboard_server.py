"""
dashboard_server.py

Live dashboard voor de Touch & Turn Scalper -- toont per symbool wat de
bewakingslus (reversal_monitor_module.py) binnenkrijgt en beslist:
welke candles er zijn opgehaald, of er een hamer-opstelling wacht op
bevestiging, en de uiteindelijke uitkomst.

Gebruikt UITSLUITEND Python's ingebouwde http.server (geen nieuwe
dependencies nodig) -- leest de monitor_state_*.json-bestanden die
reversal_monitor_module.py elke poll wegschrijft.

Draait standaard alleen op 127.0.0.1 (niet publiek bereikbaar) -- bekijk
via een SSH-tunnel, zelfde patroon als de IBKR Gateway:
    ssh -L 8899:127.0.0.1:8899 root@<jouw-vps-ip>
Open daarna in je browser: http://localhost:8899

Starten (op de VPS):
    cd /opt/strategy
    nohup python3 dashboard_server.py > logs/dashboard.log 2>&1 &
"""

import json
import os
import glob
from http.server import HTTPServer, BaseHTTPRequestHandler

LOGS_DIR = "/opt/strategy/logs"
PORT = 8899

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<title>Touch &amp; Turn Scalper -- Live Dashboard</title>
<meta charset="utf-8">
<style>
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #1a1a2e; color: #eaeaea; padding: 20px; max-width: 900px; margin: 0 auto; }
  h1 { color: #4ecdc4; font-size: 1.5em; }
  .symbol-card { background: #16213e; border-radius: 8px; padding: 16px; margin-bottom: 16px; border-left: 4px solid #ffd166; }
  .symbol-card.bevestigd { border-left-color: #06d6a0; }
  .symbol-card.verlopen { border-left-color: #ef476f; }
  .symbol-card.hamer_setup_gevonden { border-left-color: #ff9f1c; }
  .symbol-name { font-size: 1.3em; font-weight: bold; }
  .status-badge { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 0.8em; margin-left: 10px; text-transform: uppercase; }
  .status-badge.wachten { background: #ffd166; color: #000; }
  .status-badge.hamer_setup_gevonden { background: #ff9f1c; color: #000; }
  .status-badge.bevestigd { background: #06d6a0; color: #000; }
  .status-badge.verlopen { background: #ef476f; color: #fff; }
  .row { margin: 5px 0; font-size: 0.9em; }
  .label { color: #888; display: inline-block; width: 150px; }
  table { border-collapse: collapse; width: 100%; margin-top: 8px; }
  th, td { text-align: left; padding: 3px 8px; border-bottom: 1px solid #2a2a4a; font-size: 0.85em; }
  th { color: #4ecdc4; }
  .bullish { color: #06d6a0; }
  .bearish { color: #ef476f; }
  #laatste-update { color: #888; font-size: 0.85em; margin-bottom: 15px; }
  .geen-data { color: #666; font-style: italic; }
</style>
</head>
<body>
<h1>&#128200; Touch &amp; Turn Scalper -- Live Dashboard</h1>
<div id="laatste-update">Laden...</div>
<div id="symbolen"></div>

<script>
async function verversen() {
    try {
        const resp = await fetch('/api/state');
        const states = await resp.json();
        const container = document.getElementById('symbolen');
        container.innerHTML = '';
        if (states.length === 0) {
            container.innerHTML = '<p class="geen-data">Geen actieve bewakingsprocessen gevonden (mogelijk buiten handelstijd, of nog geen cyclus gedraaid).</p>';
        }
        states.forEach(s => {
            const kaart = document.createElement('div');
            kaart.className = 'symbol-card ' + (s.status || 'wachten');
            let candlesHtml = '<table><tr><th>Tijd</th><th>Open</th><th>High</th><th>Low</th><th>Close</th></tr>';
            (s.candles_today || []).forEach(c => {
                const kleurClass = c.is_bullish ? 'bullish' : 'bearish';
                candlesHtml += `<tr class="${kleurClass}"><td>${c.timestamp}</td><td>${c.open}</td><td>${c.high}</td><td>${c.low}</td><td>${c.close}</td></tr>`;
            });
            candlesHtml += '</table>';

            let signaalHtml = '';
            if (s.laatste_signaal) {
                signaalHtml = `<div class="row"><span class="label">Signaal:</span> ${s.laatste_signaal.pattern_type} -- trigger ${s.laatste_signaal.trigger_price}, SL ${s.laatste_signaal.stop_loss_price}</div>`;
            }

            kaart.innerHTML = `
                <span class="symbol-name">${s.symbol}</span>
                <span class="status-badge ${s.status || 'wachten'}">${(s.status || 'wachten').replace('_', ' ')}</span>
                <div class="row"><span class="label">Richting:</span> ${s.direction}</div>
                <div class="row"><span class="label">Box:</span> [${s.box_low}, ${s.box_high}]</div>
                <div class="row"><span class="label">Deadline:</span> ${s.deadline}</div>
                <div class="row"><span class="label">Laatste poll:</span> ${s.last_poll_at}</div>
                <div class="row"><span class="label">Wachtende hamer:</span> ${s.wachtende_hamer ? ('JA (@ ' + s.wachtende_hamer.timestamp + ', high=' + s.wachtende_hamer.high + ', low=' + s.wachtende_hamer.low + ')') : 'nee'}</div>
                ${signaalHtml}
                <div class="row"><span class="label">Candles vandaag:</span></div>
                ${candlesHtml}
            `;
            container.appendChild(kaart);
        });
        document.getElementById('laatste-update').innerText = 'Laatst ververst: ' + new Date().toLocaleTimeString('nl-NL');
    } catch (e) {
        document.getElementById('laatste-update').innerText = 'Fout bij ophalen: ' + e;
    }
}
verversen();
setInterval(verversen, 5000);
</script>
</body>
</html>"""


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send_html()
        elif self.path == "/api/state":
            self._send_json_state()
        else:
            self.send_error(404)

    def _send_json_state(self):
        states = []
        for filepath in sorted(glob.glob(os.path.join(LOGS_DIR, "monitor_state_*.json"))):
            try:
                with open(filepath) as f:
                    states.append(json.load(f))
            except Exception:
                continue  # een kapot/half-geschreven bestand mag het dashboard niet laten crashen
        body = json.dumps(states).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self):
        body = DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        pass  # onderdruk http.server's standaard-toegangslog naar de console


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), DashboardHandler)
    print(f"Dashboard draait op http://127.0.0.1:{PORT} (alleen lokaal bereikbaar -- gebruik een SSH-tunnel)")
    server.serve_forever()
