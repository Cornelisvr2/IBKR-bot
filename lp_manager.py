"""
lp_manager.py

Actief beheer van een concentrated liquidity positie in de SaucerSwap V2
HBAR/USDC-pool, specifiek voor gebruik tijdens HOLD-signalen van de
strategy_engine (rustige/neutrale sentiment-periodes). Vult het concept
in dat in PLAN.md als "later" stond genoteerd, nu gescopet naar het
enige paar dat we gebruiken: HBAR/USDC.

Kernidee (Gemini-gesprek, "Uithoudingsvermogen"-module):
- Bij HOLD: open een smalle liquiditeitspositie rond de huidige prijs
- Monitor of de prijs uit de marge loopt -> zo ja: positie sluiten,
  herbalanceren, nieuwe positie rond de nieuwe prijs
- Bij een sterk sentiment-signaal (BUY/SELL of paniek-override):
  EERST de LP-positie intrekken, dan pas de swap uitvoeren -- anders
  zit kapitaal vast in de pool tijdens een crash

BELANGRIJK -- V2 CLMM werkt met 'ticks', niet met simpele prijzen. Een
tick-index correspondeert met een specifieke prijs via de formule
price = 1.0001^tick. Deze module rekent dit voor je om, maar het
tickSpacing van de pool (afhankelijk van de fee-tier) moet kloppen --
zie DEFAULT_TICK_SPACING_BY_FEE hieronder, geverifieerd tegen Uniswap
V3's standaardwaarden (SaucerSwap V2 is een 1-op-1 fork daarvan).
"""

import math
import statistics
import requests
from dataclasses import dataclass
from typing import Optional, List
from enum import Enum

from hedera_rpc_client import HederaRpcClient


class VolatilityRegime(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


# Range-breedte per regime -- periodiek herzien (bv. elke paar uur), niet
# continu op elke prijsbeweging. Zie compute_volatility_regime().
RANGE_WIDTH_BY_REGIME = {
    VolatilityRegime.LOW: 0.03,     # smal: maximale fee-opbrengst
    VolatilityRegime.NORMAL: 0.05,  # de oorspronkelijke default
    VolatilityRegime.HIGH: 0.12,    # relaxed: minder herbalanceringen nodig
}

# Drempels op basis van rolling standaarddeviatie van uurrendementen.
# Zelfde soort aanpak als coingecko_client.py's beta-berekening, maar dan
# voor volatiliteit i.p.v. correlatie.
VOLATILITY_LOW_THRESHOLD = 0.01   # <1% stdev per uur
VOLATILITY_HIGH_THRESHOLD = 0.03  # >3% stdev per uur


def compute_volatility_regime(hourly_returns: List[float]) -> VolatilityRegime:
    """
    hourly_returns: recente uurrendementen (bv. laatste 24-48u).
    Bepaalt in welk regime we zitten -- bedoeld om periodiek (elke paar
    uur) aangeroepen te worden, niet bij elke prijs-poll, om overmatig
    herbalanceren te voorkomen.
    """
    if len(hourly_returns) < 3:
        return VolatilityRegime.NORMAL  # te weinig data, val terug op default

    vol = statistics.pstdev(hourly_returns)

    if vol < VOLATILITY_LOW_THRESHOLD:
        return VolatilityRegime.LOW
    elif vol > VOLATILITY_HIGH_THRESHOLD:
        return VolatilityRegime.HIGH
    else:
        return VolatilityRegime.NORMAL


# Uniswap V3-standaard (SaucerSwap V2 is hierop gebaseerd): elke fee-tier
# hoort bij een vaste tickSpacing. Dit MOET kloppen anders faalt mint().
DEFAULT_TICK_SPACING_BY_FEE = {
    100: 1,      # 0.01%
    500: 10,     # 0.05%
    3000: 60,    # 0.3%
    10000: 200,  # 1%
}

POSITION_MANAGER_ABI = [
    {
        "name": "mint",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params", "type": "tuple",
            "components": [
                {"name": "token0", "type": "address"},
                {"name": "token1", "type": "address"},
                {"name": "fee", "type": "uint24"},
                {"name": "tickLower", "type": "int24"},
                {"name": "tickUpper", "type": "int24"},
                {"name": "amount0Desired", "type": "uint256"},
                {"name": "amount1Desired", "type": "uint256"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "recipient", "type": "address"},
                {"name": "deadline", "type": "uint256"},
            ],
        }],
        "outputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "decreaseLiquidity",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params", "type": "tuple",
            "components": [
                {"name": "tokenId", "type": "uint256"},
                {"name": "liquidity", "type": "uint128"},
                {"name": "amount0Min", "type": "uint256"},
                {"name": "amount1Min", "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ],
        }],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "collect",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{
            "name": "params", "type": "tuple",
            "components": [
                {"name": "tokenId", "type": "uint256"},
                {"name": "recipient", "type": "address"},
                {"name": "amount0Max", "type": "uint128"},
                {"name": "amount1Max", "type": "uint128"},
            ],
        }],
        "outputs": [
            {"name": "amount0", "type": "uint256"},
            {"name": "amount1", "type": "uint256"},
        ],
    },
    {
        "name": "positions",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "outputs": [
            {"name": "nonce", "type": "uint96"},
            {"name": "operator", "type": "address"},
            {"name": "token0", "type": "address"},
            {"name": "token1", "type": "address"},
            {"name": "fee", "type": "uint24"},
            {"name": "tickLower", "type": "int24"},
            {"name": "tickUpper", "type": "int24"},
            {"name": "liquidity", "type": "uint128"},
            {"name": "feeGrowthInside0LastX128", "type": "uint256"},
            {"name": "feeGrowthInside1LastX128", "type": "uint256"},
            {"name": "tokensOwed0", "type": "uint128"},
            {"name": "tokensOwed1", "type": "uint128"},
        ],
    },
    {
        "name": "Transfer",
        "type": "event",
        "anonymous": False,
        "inputs": [
            {"name": "from", "type": "address", "indexed": True},
            {"name": "to", "type": "address", "indexed": True},
            {"name": "tokenId", "type": "uint256", "indexed": True},
        ],
    },
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
    },
    {
        "name": "refundETH",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [],
        "outputs": [],
    },
    {
        "name": "unwrapWHBAR",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountMinimum", "type": "uint256"},
            {"name": "recipient", "type": "address"},
        ],
        "outputs": [],
    },
]


def price_to_tick(price: float, token0_decimals: int, token1_decimals: int) -> int:
    """
    Zet een 'gewone' prijs (token1 per token0) om naar een tick-index.
    price = 1.0001^tick, gecorrigeerd voor het decimalenverschil tussen
    de twee tokens.

    KRITIEK GECORRIGEERD (24 aug 2026, empirisch gevonden bij de eerste
    echte LP-positie-poging): de exponent stond omgekeerd. Uniswap V3's
    interne prijs is altijd token1_raw/token0_raw (in kleinste eenheden).
    Bij WHBAR (8 dec, token0) en SAUCE (6 dec, token1) geldt:
    mensvriendelijke_prijs = interne_prijs * 10^(token0_decimals -
    token1_decimals) -- dus om van mensvriendelijk terug naar intern te
    gaan moet je delen, niet vermenigvuldigen met die factor. Geverifieerd
    met een bekend rekenvoorbeeld (1 BTC @ 30.000 USDC): de oude formule
    gaf een factor 10.000 te hoog resultaat.
    """
    adjusted_price = price * (10 ** (token1_decimals - token0_decimals))
    tick = math.log(adjusted_price) / math.log(1.0001)
    return int(round(tick))


def nearest_usable_tick(tick: int, tick_spacing: int) -> int:
    """Rondt een tick af naar het dichtstbijzijnde geldige veelvoud van tickSpacing."""
    return round(tick / tick_spacing) * tick_spacing


@dataclass
class LpPositionConfig:
    position_manager_address: str
    token0: str  # LET OP: moet alfabetisch/numeriek de kleinste van de twee adressen zijn
    token1: str
    whbar_address: Optional[str] = None  # nodig om te bepalen welke kant HBAR is (voor de payable-waarde)
    whbar_helper_address: Optional[str] = None  # nodig voor het correct unwrappen (24 aug 2026)
    factory_address: Optional[str] = None  # nodig om mintFee() op te vragen
    mirror_node_url: Optional[str] = None  # nodig voor de exchange-rate-lookup bij mintFee()
    fee_tier: int = 3000
    range_width_pct: float = 0.05  # +/- 5% rond de huidige prijs
    token0_decimals: int = 8   # WHBAR
    token1_decimals: int = 6   # USDC (of 18 op testnet, zie config.py)
    deadline_seconds: int = 120


@dataclass
class LpPositionState:
    token_id: Optional[int]
    tick_lower: Optional[int]
    tick_upper: Optional[int]
    is_open: bool = False
    last_rebalance_at: Optional[float] = None  # unix timestamp


FACTORY_MINT_FEE_ABI = [
    {
        "name": "mintFee",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],  # in tinycent (US)
    },
]


WHBAR_HELPER_ABI = [
    {
        "name": "unwrapWhbar",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
]

MINIMAL_ERC20_ABI = [
    {
        "name": "balanceOf", "type": "function", "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve", "type": "function", "stateMutability": "nonpayable",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


class LpManager:
    def __init__(self, rpc_client: HederaRpcClient, config: LpPositionConfig):
        self.rpc_client = rpc_client
        self.config = config
        self.position_manager = rpc_client.w3.eth.contract(
            address=config.position_manager_address, abi=POSITION_MANAGER_ABI
        )
        self.factory = None
        if config.factory_address:
            self.factory = rpc_client.w3.eth.contract(
                address=config.factory_address, abi=FACTORY_MINT_FEE_ABI
            )
        self.whbar_helper = None
        if config.whbar_helper_address:
            self.whbar_helper = rpc_client.w3.eth.contract(
                address=config.whbar_helper_address, abi=WHBAR_HELPER_ABI
            )
        self.whbar_token = None
        if config.whbar_address:
            self.whbar_token = rpc_client.w3.eth.contract(
                address=config.whbar_address, abi=MINIMAL_ERC20_ABI
            )
        self.state = LpPositionState(token_id=None, tick_lower=None, tick_upper=None)

        tick_spacing = DEFAULT_TICK_SPACING_BY_FEE.get(config.fee_tier)
        if tick_spacing is None:
            raise ValueError(
                f"Onbekende fee_tier {config.fee_tier} -- geen bekende tickSpacing. "
                f"Verifieer dit tegen de daadwerkelijke pool voordat je verdergaat."
            )
        self.tick_spacing = tick_spacing

    def _deadline(self) -> int:
        import time
        return int(time.time()) + self.config.deadline_seconds

    def compute_range(self, current_price: float,
                       volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL,
                       sentiment_direction: float = 0.0) -> tuple[int, int]:
        """
        Berekent tickLower/tickUpper, nu met twee onafhankelijke aanpassingen
        bovenop de basis symmetrische marge:

        1. volatility_regime bepaalt de BREEDTE (LOW=smal, HIGH=breed) --
           bedoeld om periodiek te verversen, niet elke poll-cyclus.
        2. sentiment_direction (-1.0 tot +1.0, bv. de combined_score uit
           strategy_engine) schuift de range asymmetrisch VOORUIT in de
           verwachte richting, zodat de positie minder snel uit de marge
           loopt als sentiment gelijk krijgt. Bij sentiment_direction=0
           blijft de range symmetrisch zoals voorheen.
        """
        width = RANGE_WIDTH_BY_REGIME[volatility_regime]

        # Asymmetrische verschuiving: bij sterk positief sentiment leunt de
        # range naar boven (meer ruimte om te stijgen, iets minder naar
        # beneden), en omgekeerd. skew=0.4 betekent: bij sentiment=+1.0
        # verschuift het midden van de range 40% van de breedte omhoog.
        max_skew_fraction = 0.4
        skew = sentiment_direction * max_skew_fraction * width

        lower_price = current_price * (1 - width + skew)
        upper_price = current_price * (1 + width + skew)

        tick_lower = price_to_tick(lower_price, self.config.token0_decimals, self.config.token1_decimals)
        tick_upper = price_to_tick(upper_price, self.config.token0_decimals, self.config.token1_decimals)

        tick_lower = nearest_usable_tick(tick_lower, self.tick_spacing)
        tick_upper = nearest_usable_tick(tick_upper, self.tick_spacing)

        return tick_lower, tick_upper

    def is_price_out_of_range(self, current_price: float) -> bool:
        """Checkt of de huidige prijs nog binnen de actieve positie-marge valt."""
        if not self.state.is_open:
            return False

        current_tick = price_to_tick(
            current_price, self.config.token0_decimals, self.config.token1_decimals
        )
        return current_tick < self.state.tick_lower or current_tick > self.state.tick_upper

    def _get_mint_fee_tinybar(self) -> int:
        """
        Vraagt de actuele mint-fee op (Factory.mintFee(), in tinycent US)
        en rekent 'm om naar tinybar via de mirror-node exchange-rate-API
        -- exact het patroon uit de officiele SaucerSwap-docs
        (developers/v2/liquidity/liquidity-position-fee, 23 aug 2026).

        Geeft 0 terug als factory_address of mirror_node_url ontbreekt,
        zodat de aanroeper hier zelf een beslissing over kan nemen i.p.v.
        een stille crash.
        """
        if not self.factory or not self.config.mirror_node_url:
            return 0

        tinycent = self.factory.functions.mintFee().call()
        if tinycent == 0:
            return 0

        response = requests.get(
            f"{self.config.mirror_node_url}/api/v1/network/exchangerate", timeout=10
        )
        response.raise_for_status()
        current_rate = response.json()["current_rate"]
        cent_equivalent = current_rate["cent_equivalent"]
        hbar_equivalent = current_rate["hbar_equivalent"]

        cent_to_hbar_ratio = cent_equivalent / hbar_equivalent
        tinybar = round(tinycent / cent_to_hbar_ratio)
        return tinybar

    def _ensure_token_approval(self, token_address: str, amount_raw: int):
        """Regelt een approve() voor een willekeurig (niet-WHBAR) token richting de PositionManager."""
        token_contract = self.rpc_client.w3.eth.contract(address=token_address, abi=MINIMAL_ERC20_ABI)
        approve_fn = token_contract.functions.approve(self.config.position_manager_address, amount_raw)
        approve_tx = self.rpc_client.build_and_send_transaction(approve_fn)
        self.rpc_client.wait_for_receipt(approve_tx)

    def open_position(self, amount0_desired: int, amount1_desired: int,
                       current_price: float, slippage_tolerance: float = 0.02,
                       volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL,
                       sentiment_direction: float = 0.0) -> int:
        """
        Opent een nieuwe LP-positie rond de huidige prijs. Geeft de
        token_id van de nieuwe NFT-positie terug.

        Volgt het officiele SaucerSwap-patroon (docs.saucerswap.finance,
        23 aug 2026): mint() en refundETH() gebundeld via multicall(),
        met de HBAR-kant als payable msg.value i.p.v. een aparte
        approve() -- "if the token is HBAR, no spender allowance is
        required". Zonder dit patroon ontvangt het contract nooit de
        HBAR die het intern moet wrappen, en faalt de call altijd zodra
        er een echte pool is.
        """
        # KRITIEK, empirisch gevonden 24 aug 2026: het niet-WHBAR-token
        # (bv. SAUCE, USDC) heeft een voorafgaande approve() nodig richting
        # de PositionManager -- "if the token is HBAR, no spender allowance
        # is required" geldt ALLEEN voor de HBAR-kant zelf. Zonder dit
        # faalt mint() stilzwijgend met een lege revert-reden.
        if self.config.whbar_address:
            whbar_lower = self.config.whbar_address.lower()
            if self.config.token0.lower() != whbar_lower:
                self._ensure_token_approval(self.config.token0, amount0_desired)
            if self.config.token1.lower() != whbar_lower:
                self._ensure_token_approval(self.config.token1, amount1_desired)

        tick_lower, tick_upper = self.compute_range(current_price, volatility_regime, sentiment_direction)

        params = (
            self.config.token0,
            self.config.token1,
            self.config.fee_tier,
            tick_lower,
            tick_upper,
            amount0_desired,
            amount1_desired,
            int(amount0_desired * (1 - slippage_tolerance)),
            int(amount1_desired * (1 - slippage_tolerance)),
            self.rpc_client.address,
            self._deadline(),
        )

        # Bepaal de payable HBAR-waarde: als token0 of token1 WHBAR is,
        # moet het BIJBEHORENDE amount_desired als msg.value meegestuurd
        # worden zodat het contract dat intern kan wrappen.
        # Payable-waarde: de WHBAR-kant van amount0/1_desired staat in
        # WHBAR's EIGEN kleinste eenheid (token0_decimals/token1_decimals,
        # standaard 8) -- maar msg.value wordt door de EVM-relay
        # geinterpreteerd in de 18-decimalen-wei-conventie (zelfde als
        # get_hbar_balance() elders gebruikt). Die twee verschillen, en
        # moeten hier expliciet omgerekend worden -- anders exact
        # dezelfde decimalen-fout als eerder vandaag al meermaals
        # gevonden en opgelost.
        payable_value = 0
        if self.config.whbar_address:
            whbar_lower = self.config.whbar_address.lower()
            if self.config.token0.lower() == whbar_lower:
                decimal_correction = 10 ** (18 - self.config.token0_decimals)
                payable_value = amount0_desired * decimal_correction
            elif self.config.token1.lower() == whbar_lower:
                decimal_correction = 10 ** (18 - self.config.token1_decimals)
                payable_value = amount1_desired * decimal_correction

        # Mint-fee toevoegen (in tinybar, ook omgerekend naar de
        # 18-decimalen-wei-conventie: 1 tinybar = 10^-8 HBAR = 10^10 wei).
        mint_fee_tinybar = self._get_mint_fee_tinybar()
        if mint_fee_tinybar > 0:
            payable_value += mint_fee_tinybar * (10 ** 10)

        mint_encoded = self.position_manager.encode_abi("mint", args=[params])
        refund_eth_encoded = self.position_manager.encode_abi("refundETH", args=[])

        multicall_fn = self.position_manager.functions.multicall([mint_encoded, refund_eth_encoded])
        tx_hash = self.rpc_client.build_and_send_transaction(multicall_fn, value_wei=payable_value)
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        if receipt["status"] != "success":
            raise RuntimeError(f"LP-positie openen mislukt: {tx_hash}")

        token_id = self._extract_token_id_from_mint(tx_hash)

        self.state.token_id = token_id
        self.state.tick_lower = tick_lower
        self.state.tick_upper = tick_upper
        self.state.is_open = True

        return token_id

    def _extract_token_id_from_mint(self, tx_hash: str) -> int:
        """
        Haalt de tokenId van de zojuist gemintte LP-positie-NFT op uit de
        Transfer-event-logs van de transactie (ERC721 mint = Transfer van
        het nul-adres naar de ontvanger, met tokenId als derde indexed
        argument). Betrouwbaarder dan raden of aannemen.
        """
        raw_receipt = self.rpc_client.w3.eth.get_transaction_receipt(tx_hash)
        transfer_events = self.position_manager.events.Transfer().process_receipt(raw_receipt)

        if not transfer_events:
            raise RuntimeError(
                f"Geen Transfer-event gevonden in mint-transactie {tx_hash} -- "
                f"kan tokenId niet vaststellen. Controleer handmatig via HashScan."
            )

        # Bij een mint is er precies één Transfer-event (van 0x0 naar de ontvanger).
        return transfer_events[0]["args"]["tokenId"]

    def close_position(self, token_id: int, liquidity: Optional[int] = None) -> str:
        """
        Trekt alle liquiditeit terug en int de opgebouwde fees.

        DEFINITIEF GECORRIGEERD (24 aug 2026, empirisch bevestigd op
        testnet): de PositionManager's eigen unwrapWHBAR() (net als de
        SwapRouter's variant) unwrapt NIET wat al naar de gebruiker is
        gestuurd via collect(). De eerdere multicall-bundeling met
        unwrapWHBAR loste dus niets op.

        De WERKELIJK correcte route: decrease + collect (die stuurt de
        WHBAR al naar de gebruiker via recipient=self.rpc_client.address),
        en DAARNA een aparte aanroep naar WhbarHelper.unwrapWhbar() (die
        zelf weer een voorafgaande approve() nodig heeft, want WhbarHelper
        haalt de WHBAR actief op via safeTransferFrom()).

        liquidity: als niet opgegeven, wordt de actuele waarde eerst
        opgehaald via positions(tokenId).
        """
        if liquidity is None:
            position_data = self.position_manager.functions.positions(token_id).call()
            liquidity = position_data[7]

        calls = []

        if liquidity > 0:
            decrease_params = (token_id, liquidity, 0, 0, self._deadline())
            calls.append(self.position_manager.encode_abi("decreaseLiquidity", args=[decrease_params]))

        collect_params = (token_id, self.rpc_client.address, 2**128 - 1, 2**128 - 1)
        calls.append(self.position_manager.encode_abi("collect", args=[collect_params]))

        multicall_fn = self.position_manager.functions.multicall(calls)
        tx_hash = self.rpc_client.build_and_send_transaction(multicall_fn)
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        self.state = LpPositionState(token_id=None, tick_lower=None, tick_upper=None, is_open=False)

        if receipt["status"] != "success" or not self.whbar_helper or not self.whbar_token:
            return tx_hash if receipt["status"] == "success" else None

        # Positie gesloten -- nu de daadwerkelijk ontvangen WHBAR unwrappen
        # (indien deze pool WHBAR bevatte; anders is de balans gewoon 0).
        whbar_balance_raw = self.whbar_token.functions.balanceOf(self.rpc_client.address).call()
        if whbar_balance_raw == 0:
            return tx_hash

        approve_fn = self.whbar_token.functions.approve(
            self.config.whbar_helper_address, whbar_balance_raw
        )
        approve_tx = self.rpc_client.build_and_send_transaction(approve_fn)
        self.rpc_client.wait_for_receipt(approve_tx)

        unwrap_fn = self.whbar_helper.functions.unwrapWhbar(whbar_balance_raw)
        unwrap_tx = self.rpc_client.build_and_send_transaction(unwrap_fn, gas_limit=1_000_000)
        self.rpc_client.wait_for_receipt(unwrap_tx)

        return tx_hash

    def rebalance_if_needed(self, current_price: float, amount0: int, amount1: int,
                              volatility_regime: VolatilityRegime = VolatilityRegime.NORMAL,
                              sentiment_direction: float = 0.0,
                              proactive_threshold: float = 0.6,
                              cooldown_seconds: float = 4 * 3600) -> bool:
        """
        Twee triggers voor herbalanceren, allebei onderworpen aan dezelfde
        cooldown om te voorkomen dat de proactieve sentiment-trigger en de
        reactieve prijs-trigger elkaar opjagen tot te frequent herbalanceren:

        1. REACTIEF: de prijs is al buiten de huidige marge.
        2. PROACTIEF: sentiment is sterk genoeg om de range vast te
           verschuiven vóórdat de prijs er daadwerkelijk buiten valt.

        cooldown_seconds: baseline-waarde (4 uur), nog NIET gekalibreerd
        op historische data -- dat vereist SaucerSwap-poolvolumedata
        (niet alleen prijs) om een echte kosten/baten-afweging te maken
        tussen gemiste fees (te wijde marge) en gas/misgelopen fee-tijd
        (te frequent herbalanceren). Zie PLAN.md voor de vervolgstap
        zodra die data beschikbaar is.

        Geeft True terug als er geherbalanceerd is.
        """
        import time

        if self.state.last_rebalance_at is not None:
            elapsed = time.time() - self.state.last_rebalance_at
            if elapsed < cooldown_seconds:
                return False  # cooldown actief, ongeacht trigger-type

        needs_reactive_rebalance = self.is_price_out_of_range(current_price)
        needs_proactive_rebalance = (
            self.state.is_open and abs(sentiment_direction) >= proactive_threshold
        )

        if not needs_reactive_rebalance and not needs_proactive_rebalance:
            return False

        if self.state.token_id is not None:
            self.close_position(self.state.token_id)  # liquidity wordt nu automatisch opgehaald
            # LET OP (23 aug 2026, zelfde categorie fix als regime_orchestrator.py):
            # amount0/amount1 zijn de oorspronkelijk MEEGEGEVEN parameters,
            # niet wat er daadwerkelijk uit close_position() terugkwam
            # (inclusief opgebouwde fees, en een mogelijk andere token0/
            # token1-verhouding door prijsbeweging binnen de oude range).
            # De aanroeper (LpOrchestrator, momenteel inactief sinds de
            # RegimeOrchestrator-pivot) moet daarom na een rebalance de
            # WERKELIJKE wallet-balans opvragen en die gebruiken, niet
            # simpelweg dezelfde amount0/amount1 doorgeven aan
            # open_position() hieronder. Deze functie zelf kan dat niet
            # oplossen zonder een balans-query, die hoort in de aanroeper
            # thuis (zie RegimeOrchestrator._get_swappable_hbar_balance()
            # /_get_swappable_usdc_balance() als voorbeeld-patroon).

        self.open_position(
            amount0, amount1, current_price,
            volatility_regime=volatility_regime,
            sentiment_direction=sentiment_direction,
        )
        self.state.last_rebalance_at = time.time()
        return True


if __name__ == "__main__":
    config = LpPositionConfig(
        position_manager_address="0x0000000000000000000000000000000000000001",
        token0="0x0000000000000000000000000000000000000002",
        token1="0x0000000000000000000000000000000000000003",
        fee_tier=3000,
    )

    current_price = 0.065

    scenarios = [
        ("Normale volatiliteit, geen sentiment", VolatilityRegime.NORMAL, 0.0),
        ("Lage volatiliteit, geen sentiment (smalle range)", VolatilityRegime.LOW, 0.0),
        ("Hoge volatiliteit, geen sentiment (brede range)", VolatilityRegime.HIGH, 0.0),
        ("Normale volatiliteit, sterk positief sentiment (range schuift omhoog)", VolatilityRegime.NORMAL, 0.9),
        ("Hoge volatiliteit, sterk negatief sentiment (breed + naar beneden)", VolatilityRegime.HIGH, -0.9),
    ]

    for label, regime, sentiment in scenarios:
        tick_lower_raw = None
        width = RANGE_WIDTH_BY_REGIME[regime]
        skew = sentiment * 0.4 * width
        lower_price = current_price * (1 - width + skew)
        upper_price = current_price * (1 + width + skew)
        print(f"\n{label}")
        print(f"  Range: {lower_price:.4f} -- {upper_price:.4f} (breedte={width:.0%}, skew={skew:+.3f})")

    print("\n--- Volatiliteits-regime detectie testen ---")
    low_vol_returns = [0.002, -0.003, 0.001, 0.004, -0.002]
    high_vol_returns = [0.05, -0.08, 0.06, -0.04, 0.07]
    print(f"Lage-vol returns -> regime: {compute_volatility_regime(low_vol_returns).value}")
    print(f"Hoge-vol returns -> regime: {compute_volatility_regime(high_vol_returns).value}")
