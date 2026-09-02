# Kritieke bugfix: verkeerde openingscandle-selectie (24 aug 2026)

## Wat er mis was

Zowel `data_module.get_opening_candle()` (Touch & Turn Scalper) als
`vix_rider_entry_module.calculate_opening_range()` (VIX Rider) namen
domweg **de eerste candle(s) uit een teruggekeken lijst** (`candles[0]`
resp. `candles[:2]`), zonder te controleren of die candles daadwerkelijk
van **vandaag** waren.

**Praktisch gevolg:** als een cyclus rond of vlak na marktopening
draaide (voordat vandaag's eerste 15-minuten-candle daadwerkelijk was
voltooid), pakte het systeem een **oude candle van een vorige
handelsdag** en behandelde die als "de openingscandle van vandaag" --
zonder enige foutmelding. De strategie nam dus beslissingen op basis
van verkeerde, verouderde marktdata.

## Hoe het ontdekt werd

De gebruiker merkte terecht op dat een NVDA-trade op 24 augustus
binnen enkele seconden na de cron-trigger (15:15 CEST) werd geplaatst
-- onmogelijk snel voor een strategie die eerst op een voltooide
openingscandle moet wachten. Dat leidde tot onderzoek en bevestiging
van de bug.

## Twee samenhangende problemen, allebei gerepareerd

### 1. Ontbrekende datumfilter (data-logica)

**`data_module.get_opening_candle()`** filtert nu expliciet op
`candle.timestamp.date() == vandaag` voordat de eerste candle wordt
gekozen. Geeft `None` terug als er geen candles van vandaag zijn.

**`vix_rider_entry_module.calculate_opening_range()`** doet hetzelfde:
filtert eerst op vandaag's candles, geeft een `ValueError` als er te
weinig zijn.

Beide functies zijn getest met een specifiek "bug-reproductie"-scenario
(alleen oude candles, geen candles van vandaag) om te bevestigen dat
ze nu correct `None`/`ValueError` teruggeven in plaats van een oude
candle te misbruiken.

### 2. Verkeerde cron-timing (mismatch met de eigen strategie-logica)

De cron-triggers waren simpelweg te vroeg ingesteld t.o.v. wanneer de
benodigde candle daadwerkelijk bestaat:

| Strategie | Vereiste periode | Oude cron-tijd | Nieuwe cron-tijd |
|---|---|---|---|
| Scalper (15-min openingscandle) | 15:30-15:45 CEST | 15:15 en 15:30 (beide te vroeg) | **15:46** |
| VIX Rider (30-min Opening Range) | 15:30-16:00 CEST | 15:29 (te vroeg) | **16:00** |

## Belangrijke, niet-volledig-geverifieerde aanname

De datumvergelijking (`candle.timestamp.date() == vandaag`) gaat ervan
uit dat IBKR's candle-timestamps in dezelfde tijdzone staan als de
VPS's lokale tijd (Europe/Amsterdam / CEST). Dit is gebaseerd op een
eerdere observatie (de laatste candle van een handelsdag kwam overeen
met 21:45 lokale tijd, consistent met een marktsluiting om 22:00 CEST)
maar is niet expliciet met IBKR-documentatie bevestigd. Als dit ooit
niet blijkt te kloppen (bijv. rond de overgang zomer-/wintertijd, of
als IBKR UTC-timestamps teruggeeft), zou de datumfilter subtiel verkeerd
kunnen gaan rond middernacht-grensgevallen. Niet urgent, maar het
verdient een keer expliciete verificatie (bijv. door een candle-
timestamp te vergelijken met de bekende, exacte marktopeningstijd).

## Nog te overwegen (niet opgelost vandaag)

- **Geen beurskalender/feestdagen-check**: als de markt op een
  bepaalde dag gesloten is (Amerikaanse feestdag), zullen beide
  strategieën simpelweg geen candles van "vandaag" vinden en netjes
  overslaan (dankzij deze fix) -- maar dit is nooit expliciet getest
  tegen een echte feestdag.
- **VIX Rider's cron-trigger (16:00) is nu het VROEGST mogelijke
  moment** -- er is geen marge ingebouwd voor het geval IBKR's data
  een paar minuten vertraging heeft. Zou eventueel iets later (bijv.
  16:02) gezet kunnen worden voor wat speling.
