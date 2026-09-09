# Dashboard uitrollen (HTTPS via Caddy)

## 0. Eerst controleren
```
cd /opt/strategy && git status
```
Staan hier lokale, niet-gecommitte wijzigingen (m.n. order_module.py / chart_module.py)?
Commit of stash ze eerst, anders overschrijft de pull ze.

## 1. Code
```
git pull
mkdir -p data logs/charts
python3 rvb_strategy_module.py          # zelftest, moet "geslaagd" geven
```
Het oude `dashboard_server.py` (TTS-monitor op poort 8899) is door het nieuwe
bestand vervangen. Draaide het nog via `nohup`? Dan eerst stoppen:
`pkill -f dashboard_server.py`.

## 2. Dashboard als service
```
cp ibkr-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now ibkr-dashboard
curl -s http://127.0.0.1:8899/health      # -> ok
```

## 3. Caddy (HTTPS)

**Situatie A -- de HBAR-bot draait op een ANDERE VPS**
```
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
caddy hash-password --plaintext 'jouw-wachtwoord'   # hash in Caddyfile.ibkr plakken
sed "s/<VPS-IP>/$(hostname -I | awk '{print $1}' | tr . -)/" Caddyfile.ibkr > /etc/caddy/Caddyfile
systemctl reload caddy
```
Poorten 80 en 443 moeten open staan in de Hostinger-firewall.

**Situatie B -- zelfde VPS als de HBAR-bot (Caddy draait al in Docker op 80/443)**
Een tweede Caddy kan die poorten niet ook claimen. Voeg het site-blok uit
`Caddyfile.ibkr` toe aan de bestaande `Caddyfile` van de HBAR-bot, maar met
het host-adres van Docker i.p.v. 127.0.0.1:
```
ibkr.<VPS-IP>.sslip.io {
    basic_auth { ... }
    reverse_proxy host.docker.internal:8899
}
```
en in `docker-compose.yml` bij de `caddy`-service:
```
    extra_hosts:
      - "host.docker.internal:host-gateway"
```
Daarna `docker compose up -d caddy`. Omdat het dashboard alleen op 127.0.0.1
luistert, moet `HOST` in dashboard_server.py in dit geval `0.0.0.0` zijn
**én** poort 8899 dicht in de firewall (anders staat hij zonder login open).
Zet daarvoor in `/etc/environment`: `DASHBOARD_BIND=0.0.0.0`.

## 4. Controleren
Open https://ibkr.<VPS-IP>.sslip.io -- eerste keer duurt ~10 s (certificaat).

## 5. Telegram terugschroeven (nog te doen, apart)
Nu het dashboard er is: per-trade-meldingen uit, alleen alarmen
(sessie/auth, onbeschermde positie) + één dagsamenvatting om 22:05 met link.
