"""
news_module.py — Touch & Turn Scalper, Module 6: Aandeelselectie via Nieuws

Kiest, vóór markt­opening, het meest kansrijke aandeel om die dag op te
handelen: aandelen met veel nieuwsvolume/sterk sentiment hebben een
grotere kans op een significante openingsbeweging (nodig voor de
ATR-validatie in module 2).

Opbouw:
    - score_symbol_news(): pure scoring-logica op basis van nieuwsitems
      (aantal berichten + gemiddeld sentiment). Volledig testbaar
      zonder API-key.
    - select_symbol(): kiest het hoogst scorende symbool uit een lijst
      kandidaten; valt terug op FALLBACK_WATCHLIST als er geen
      bruikbare nieuwsdata is.
    - fetch_news_for_symbol(): de daadwerkelijke API-aanroep (bijv.
      Finnhub). Vereist een API-key en is NIET getest in dit gesprek
      -- dat vereist een losse validatie zodra je een key hebt.

Gebruik in andere modules:
    from news_module import select_symbol, FALLBACK_WATCHLIST
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("news_module")

# Vaste vangnet-lijst: liquide Amerikaanse aandelen met hoog volume en
# veel institutionele/algoritmische orderflow -- de doelgroep voor
# opening-liquiditeitsvegen (stop-hunts) waar deze strategie op mikt.
#
# Focus bewust op de VS (geen AEX meer): lagere handelskosten per order
# bij IBKR, en een groter en consistenter dagelijks nieuwsvolume.
#
# 23 namen i.p.v. de eerdere 8, om de kans te vergroten dat minstens 1
# van de gekozen kandidaten een geldige ATR-validatie haalt op een
# gemiddelde dag -- ruim binnen de Finnhub-limiet van 60 aanroepen/min.
FALLBACK_WATCHLIST = [
    # Grote tech (hoog optievolume)
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD",
    "NFLX", "CRM", "ADBE", "AVGO", "ORCL", "CSCO", "INTC", "QCOM",
    # Grote financials (zwaar institutioneel gehandeld)
    "JPM", "BAC", "GS", "V", "MA",
    # Hoge volatiliteit, veel retail+institutioneel volume
    "COIN", "PLTR", "MSTR",
    # Energie (andere sector-correlatie)
    "XOM", "CVX",
]


@dataclass
class NewsItem:
    """Eén nieuwsbericht over een symbool."""
    headline: str
    sentiment: float  # -1.0 (zeer negatief) tot +1.0 (zeer positief)


@dataclass
class SymbolScore:
    """Het resultaat van de nieuwsscoring voor één symbool."""
    symbol: str
    news_count: int
    avg_sentiment: float
    score: float
    reason: str

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "news_count": self.news_count,
            "avg_sentiment": self.avg_sentiment,
            "score": self.score,
            "reason": self.reason,
        }


def score_symbol_news(symbol: str, news_items: list[NewsItem]) -> SymbolScore:
    """
    Berekent een score voor een symbool op basis van nieuwsvolume en
    sentimentsterkte (absolute waarde -- zowel sterk positief als
    sterk negatief nieuws duidt op een kans op een grote koersbeweging).

    Score = aantal_berichten x gemiddelde_absolute_sentimentsterkte

    Dit is pure logica, geen API nodig -- volledig testbaar.
    """
    if not news_items:
        return SymbolScore(
            symbol=symbol, news_count=0, avg_sentiment=0.0, score=0.0,
            reason="Geen nieuwsberichten gevonden.",
        )

    avg_sentiment = sum(item.sentiment for item in news_items) / len(news_items)
    avg_abs_sentiment = sum(abs(item.sentiment) for item in news_items) / len(news_items)
    score = len(news_items) * avg_abs_sentiment

    return SymbolScore(
        symbol=symbol,
        news_count=len(news_items),
        avg_sentiment=round(avg_sentiment, 3),
        score=round(score, 3),
        reason=(
            f"{len(news_items)} berichten, gemiddeld sentiment {avg_sentiment:+.2f}, "
            f"score {score:.2f}"
        ),
    )


def select_symbol(
    candidate_scores: list[SymbolScore],
    min_score: float = 0.5,
    fallback_watchlist: list[str] = None,
) -> tuple[str, str]:
    """
    Kiest het hoogst scorende symbool uit de kandidaten. Valt terug op
    de eerste van fallback_watchlist als geen enkele kandidaat de
    minimumscore haalt (of als er geen kandidaten zijn).

    Returns:
        (gekozen_symbool, reden) -- reden is bruikbaar voor een
        Telegram-melding vóór markt­opening.
    """
    if fallback_watchlist is None:
        fallback_watchlist = FALLBACK_WATCHLIST

    if candidate_scores:
        best = max(candidate_scores, key=lambda s: s.score)
        if best.score >= min_score:
            reason = f"{best.symbol} gekozen op basis van nieuws: {best.reason}"
            logger.info(reason)
            return best.symbol, reason

    fallback_symbol = fallback_watchlist[0]
    reason = (
        f"Geen kandidaat haalde de minimumscore ({min_score}) -- "
        f"teruggevallen op vangnet-lijst: {fallback_symbol}"
    )
    logger.warning(reason)
    return fallback_symbol, reason


# Simpele, transparante sleutelwoorden-lijsten voor sentiment op
# Engelstalige nieuwskoppen (Finnhub-koppen zijn doorgaans Engels,
# ook voor Europese aandelen). Dit is bewust een lichte heuristiek --
# geen NLP-model -- maar kost niets extra en is volledig doorzichtig
# in wat het wel/niet als signaal ziet.
POSITIVE_KEYWORDS = [
    "beats", "beat estimates", "record profit", "record revenue", "upgrade",
    "raises guidance", "raises forecast", "strong demand", "surges", "rally",
    "outperform", "buy rating", "exceeds expectations", "record orders",
    "profit jump", "growth accelerates",
]
NEGATIVE_KEYWORDS = [
    "misses", "miss estimates", "downgrade", "cuts guidance", "cuts forecast",
    "warns", "profit warning", "weak demand", "plunges", "sell rating",
    "underperform", "falls short", "layoffs", "investigation", "lawsuit",
    "recall", "delays", "loss widens",
]


def score_headline_sentiment(headline: str) -> float:
    """
    Bepaalt een simpele sentimentscore voor één nieuwskop, op basis
    van het voorkomen van positieve/negatieve sleutelwoorden.

    Score = (aantal_positieve_matches - aantal_negatieve_matches) /
            max(1, totaal_aantal_matches)

    Resultaat ligt tussen -1.0 (puur negatief) en +1.0 (puur positief).
    Een kop zonder matches krijgt 0.0 (neutraal/onbekend) -- dit is een
    bewuste, herkenbare beperking van een sleutelwoorden-aanpak: subtiele
    of impliciete sentiment wordt gemist.
    """
    headline_lower = headline.lower()
    pos_matches = sum(1 for kw in POSITIVE_KEYWORDS if kw in headline_lower)
    neg_matches = sum(1 for kw in NEGATIVE_KEYWORDS if kw in headline_lower)

    total_matches = pos_matches + neg_matches
    if total_matches == 0:
        return 0.0

    return (pos_matches - neg_matches) / total_matches


def fetch_news_for_symbol(symbol: str, api_key: str, lookback_days: int = 7, max_retries: int = 2) -> list[NewsItem]:
    """
    Haalt recent nieuws op voor een symbool via Finnhub's GRATIS
    /company-news endpoint, en bepaalt per kop een sentimentscore via
    score_headline_sentiment() (sleutelwoorden-heuristiek).

    (Het rijkere /news-sentiment endpoint bleek bij testen een premium-
    only endpoint te zijn geworden -- vandaar deze aanpak in plaats
    daarvan, die wel op de gratis laag werkt.)

    Vangt specifiek 429 (Too Many Requests) af en probeert het na een
    korte pauze opnieuw -- ontdekt bij live gebruik (20 aug 2026) dat
    twee snel-achtereenvolgende cycli (bijv. dry-run + live-run) tegen
    Finnhub's limiet van 60 requests/minuut konden aanlopen bij het
    doorlopen van de volledige 26-symbolen-watchlist. Respecteert de
    Retry-After header als Finnhub die meegeeft, anders een vaste
    fallback-pauze.
    """
    import requests
    import time
    from datetime import datetime, timedelta

    to_date = datetime.utcnow().date()
    from_date = to_date - timedelta(days=lookback_days)

    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": symbol,
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "token": api_key,
    }

    attempt = 0
    while True:
        response = requests.get(url, params=params, timeout=10)

        if response.status_code == 429 and attempt < max_retries:
            wait_seconds = int(response.headers.get("Retry-After", 15))
            logger.warning(
                f"Finnhub rate limit geraakt voor {symbol} -- {wait_seconds}s wachten "
                f"en opnieuw proberen (poging {attempt + 1}/{max_retries})."
            )
            time.sleep(wait_seconds)
            attempt += 1
            continue

        response.raise_for_status()
        break

    raw_items = response.json()

    news_items = [
        NewsItem(
            headline=item.get("headline", ""),
            sentiment=score_headline_sentiment(item.get("headline", "")),
        )
        for item in raw_items
    ]

    logger.info(f"{len(news_items)} nieuwsberichten opgehaald voor {symbol}")
    return news_items


def select_top_symbols(
    candidate_scores: list[SymbolScore],
    n: int = 3,
    min_score: float = 0.5,
    fallback_watchlist: list[str] = None,
) -> list[tuple[str, str]]:
    """
    Kiest de top `n` hoogst scorende, VERSCHILLENDE symbolen uit de
    kandidaten -- voor het draaien van meerdere trades per sessie op
    verschillende aandelen (in plaats van select_symbol(), die er
    maar één teruggeeft).

    Kandidaten die de minimumscore niet halen, worden genegeerd. Als
    er na filtering minder dan `n` kandidaten overblijven, wordt de
    rest aangevuld vanuit fallback_watchlist (in volgorde, zonder
    symbolen te dupliceren die al gekozen zijn).

    Returns:
        Lijst van (symbool, reden)-tupels, maximaal `n` lang, met
        unieke symbolen.
    """
    if fallback_watchlist is None:
        fallback_watchlist = FALLBACK_WATCHLIST

    # Sorteer kandidaten die de drempel halen, hoogste score eerst.
    qualifying = sorted(
        (s for s in candidate_scores if s.score >= min_score),
        key=lambda s: s.score,
        reverse=True,
    )

    chosen: list[tuple[str, str]] = []
    chosen_symbols: set[str] = set()

    for s in qualifying:
        if len(chosen) >= n:
            break
        if s.symbol in chosen_symbols:
            continue
        reason = f"{s.symbol} gekozen op basis van nieuws: {s.reason}"
        chosen.append((s.symbol, reason))
        chosen_symbols.add(s.symbol)

    # Vul aan vanuit de vangnet-lijst als er nog plekken over zijn.
    for fallback_symbol in fallback_watchlist:
        if len(chosen) >= n:
            break
        if fallback_symbol in chosen_symbols:
            continue
        reason = f"{fallback_symbol} aangevuld vanuit vangnet-lijst (onvoldoende nieuwskandidaten)."
        chosen.append((fallback_symbol, reason))
        chosen_symbols.add(fallback_symbol)

    if not chosen:
        logger.warning("Geen enkel symbool kon gekozen worden, zelfs niet via de vangnet-lijst.")
    else:
        logger.info(f"Top {len(chosen)} symbolen gekozen: {[s for s, _ in chosen]}")

    return chosen


def select_top_symbols_from_watchlist(
    watchlist: list[str],
    api_key: str,
    n: int = 3,
    min_score: float = 0.5,
    delay_between_calls: float = 1.1,
) -> list[tuple[str, str]]:
    """
    Haalt voor elk symbool in de watchlist het nieuws op (via het
    gratis /company-news endpoint) en kiest de top `n` verschillende
    symbolen -- de functie die main.py aanroept voor de dagelijkse
    selectie van tot 3 trades.

    Args:
        delay_between_calls: pauze in seconden tussen elke Finnhub-
            aanroep (standaard 1,1s). Bij 26 symbolen kost dit ~29
            seconden extra per cyclus, maar voorkomt proactief dat de
            gratis-laag-limiet van 60 requests/minuut geraakt wordt --
            vooral relevant als een dry-run en live-run kort na elkaar
            draaien (ontdekt bij live gebruik op 20 aug 2026).
            fetch_news_for_symbol() vangt een eventuele 429 zelf ook
            nog af als extra vangnet, dus dit hoeft niet waterdicht
            te zijn.
    """
    import time

    scores = []
    for i, symbol in enumerate(watchlist):
        try:
            news_items = fetch_news_for_symbol(symbol, api_key)
            score = score_symbol_news(symbol, news_items)
            scores.append(score)
        except Exception as e:
            logger.warning(f"Kon nieuws voor {symbol} niet ophalen: {e}")

        if delay_between_calls > 0 and i < len(watchlist) - 1:
            time.sleep(delay_between_calls)

    return select_top_symbols(scores, n=n, min_score=min_score, fallback_watchlist=watchlist)


def select_symbol_from_watchlist(
    watchlist: list[str],
    api_key: str,
    min_score: float = 0.5,
) -> tuple[str, str]:
    """
    Haalt voor elk symbool in de watchlist het nieuws op (via het
    gratis /company-news endpoint), scoort elk symbool, en kiest de
    winnaar. Behouden voor gevallen waar je maar 1 symbool wilt
    (select_top_symbols_from_watchlist is de variant voor meerdere).
    """
    scores = []
    for symbol in watchlist:
        try:
            news_items = fetch_news_for_symbol(symbol, api_key)
            score = score_symbol_news(symbol, news_items)
            scores.append(score)
        except Exception as e:
            logger.warning(f"Kon nieuws voor {symbol} niet ophalen: {e}")

    return select_symbol(scores, min_score=min_score, fallback_watchlist=watchlist)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Scenario 1: drie kandidaten met verschillende nieuwsprofielen
    asml_news = [
        NewsItem("ASML rapporteert recordorders", 0.8),
        NewsItem("Chipsector onder druk door exportregels", -0.6),
        NewsItem("Analisten verhogen koersdoel ASML", 0.7),
    ]
    aapl_news = [
        NewsItem("Apple lanceert nieuw product", 0.3),
    ]
    ing_news: list[NewsItem] = []  # geen nieuws vandaag

    scores = [
        score_symbol_news("ASML", asml_news),
        score_symbol_news("AAPL", aapl_news),
        score_symbol_news("INGA", ing_news),
    ]
    for s in scores:
        print(f"Score: {s.to_dict()}")

    gekozen, reden = select_symbol(scores)
    print(f"\nScenario 1 (duidelijke winnaar): gekozen={gekozen}")
    print(f"Reden: {reden}")

    # Scenario 2: alle kandidaten scoren te laag -> fallback
    zwakke_scores = [
        score_symbol_news("ASML", [NewsItem("Kleine mededeling", 0.05)]),
        score_symbol_news("AAPL", []),
    ]
    gekozen, reden = select_symbol(zwakke_scores)
    print(f"\nScenario 2 (fallback): gekozen={gekozen}")
    print(f"Reden: {reden}")

    # Scenario 4: sleutelwoorden-sentiment op losse koppen testen
    print(f"\nScenario 4 (positieve kop): {score_headline_sentiment('ASML beats estimates, raises guidance for Q3')}")
    print(f"Scenario 5 (negatieve kop): {score_headline_sentiment('ASML warns of weak demand, cuts forecast')}")
    print(f"Scenario 6 (gemengde kop): {score_headline_sentiment('ASML beats estimates but warns of headwinds')}")
    print(f"Scenario 7 (neutrale kop, geen matches): {score_headline_sentiment('ASML announces annual shareholder meeting')}")

    # Scenario 8: volledige keten, met simulatie van wat fetch_news_for_symbol
    # zou opleveren (geen live API-aanroep nodig -- alleen de headline-parsing
    # en scoring-logica, die identiek is aan wat de live functie ook gebruikt)
    voorbeeld_koppen = [
        "ASML beats estimates, record profit reported",
        "ASML raises guidance after strong demand",
        "Chipsector faces new export regulation warns analyst",
    ]
    simulatie_items = [NewsItem(h, score_headline_sentiment(h)) for h in voorbeeld_koppen]
    simulatie_score = score_symbol_news("ASML", simulatie_items)
    print(f"\nScenario 8 (volledige keten, gesimuleerd): {simulatie_score.to_dict()}")

    # Scenario 9: top-3 kiezen uit 5 kandidaten met verschillende scores
    kandidaten = [
        SymbolScore("AAPL", 5, 0.6, 3.0, "sterk"),
        SymbolScore("MSFT", 3, 0.8, 2.4, "sterk"),
        SymbolScore("NVDA", 10, 0.9, 9.0, "zeer sterk"),
        SymbolScore("AMZN", 1, 0.1, 0.1, "te zwak"),
        SymbolScore("TSLA", 4, 0.7, 2.8, "sterk"),
    ]
    top3 = select_top_symbols(kandidaten, n=3)
    print(f"\nScenario 9 (top 3 van 5 kandidaten): {[s for s, _ in top3]}")
    print("(verwacht: NVDA, AAPL, TSLA -- op volgorde van score 9.0 > 3.0 > 2.8, AMZN afgevallen op score)")

    # Scenario 10: te weinig kwalificerende kandidaten -> aangevuld vanuit fallback
    weinig_kandidaten = [SymbolScore("AAPL", 5, 0.6, 3.0, "sterk")]
    top3_aangevuld = select_top_symbols(weinig_kandidaten, n=3, fallback_watchlist=["MSFT", "NVDA", "AAPL", "AMZN"])
    print(f"\nScenario 10 (aangevuld vanuit fallback): {[s for s, _ in top3_aangevuld]}")
    print("(verwacht: AAPL (kwalificeert), dan MSFT, NVDA vanuit fallback -- AAPL niet dubbel)")
