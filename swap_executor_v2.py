"""
swap_executor_v2.py

Voert HBAR <-> USDC swaps uit via SaucerSwap V2 (concentrated liquidity,
Uniswap V3-stijl architectuur). Fundamenteel anders dan V1:
- Geen simpele address[]-path, maar exactInputSingle met een fee-tier
- Quotes via QuoterV2.quoteExactInputSingle (LET OP: dit is geen pure
  'view'-call -- de functie is technisch 'non-view' omdat hij intern een
  swap simuleert en revert. web3.py's .call() werkt hier nog steeds
  (simuleert zonder daadwerkelijk te versturen), maar reken niet op een
  vaste lage gas-cost zoals bij een echte view-call.

ABI-fragment direct afgeleid van:
  github.com/saucerswaplabs/saucerswaplabs-v2-periphery
  (contracts/interfaces/ISwapRouter.sol en IQuoterV2.sol)

BELANGRIJK: fee-tier moet overeenkomen met een daadwerkelijk bestaande
HBAR/USDC-pool. Typische Uniswap V3-stijl tiers zijn 500 (0.05%),
3000 (0.3%), 10000 (1%) -- maar SaucerSwap kan afwijkende tiers gebruiken.
Verifieer dit met de Factory's getPool(tokenA, tokenB, fee) voordat je
een fee-tier hardcodeert; een verkeerde tier levert gewoon een revert op
(geen fondsverlies, maar wel een mislukte trade).
"""

import time
from dataclasses import dataclass
from typing import Optional

from hedera_rpc_client import HederaRpcClient


WHBAR_HELPER_ABI = [
    {
        "name": "unwrapWhbar",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "wad", "type": "uint256"}],
        "outputs": [],
    },
]

SWAP_ROUTER_ABI = [
    {
        "name": "exactInputSingle",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "recipient", "type": "address"},
                    {"name": "deadline", "type": "uint256"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "amountOutMinimum", "type": "uint256"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [{"name": "amountOut", "type": "uint256"}],
    },
    {
        "name": "multicall",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [{"name": "data", "type": "bytes[]"}],
        "outputs": [{"name": "results", "type": "bytes[]"}],
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

QUOTER_V2_ABI = [
    {
        "name": "quoteExactInputSingle",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {
                "name": "params",
                "type": "tuple",
                "components": [
                    {"name": "tokenIn", "type": "address"},
                    {"name": "tokenOut", "type": "address"},
                    {"name": "amountIn", "type": "uint256"},
                    {"name": "fee", "type": "uint24"},
                    {"name": "sqrtPriceLimitX96", "type": "uint160"},
                ],
            }
        ],
        "outputs": [
            {"name": "amountOut", "type": "uint256"},
            {"name": "sqrtPriceX96After", "type": "uint160"},
            {"name": "initializedTicksCrossed", "type": "uint32"},
            {"name": "gasEstimate", "type": "uint256"},
        ],
    },
]



ERC20_ABI = [
    {
        "name": "balanceOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approve",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


@dataclass
class SwapConfigV2:
    swap_router_address: str
    quoter_v2_address: str
    usdc_address: str
    whbar_address: str
    whbar_helper_address: Optional[str] = None  # nodig voor het correct unwrappen (24 aug 2026)
    usdc_decimals: int = 6
    whbar_decimals: int = 8  # bevestigd 23 aug 2026 via een echte quote tegen de WHBAR/SAUCE-pool
    fee_tier: int = 3000  # 0.3% -- VERIFIEER dit tegen de daadwerkelijke pool
    slippage_tolerance: float = 0.01
    deadline_seconds: int = 120


@dataclass
class SwapResultV2:
    tx_hash: str
    status: str
    amount_in: float
    estimated_amount_out: float
    direction: str
    fee_tier: int


class SwapExecutorV2:
    def __init__(self, rpc_client: HederaRpcClient, config: SwapConfigV2):
        self.rpc_client = rpc_client
        self.config = config
        self.router = rpc_client.w3.eth.contract(
            address=config.swap_router_address, abi=SWAP_ROUTER_ABI
        )
        self.quoter = rpc_client.w3.eth.contract(
            address=config.quoter_v2_address, abi=QUOTER_V2_ABI
        )
        self.usdc = rpc_client.w3.eth.contract(address=config.usdc_address, abi=ERC20_ABI)
        self.whbar_token = rpc_client.w3.eth.contract(address=config.whbar_address, abi=ERC20_ABI)
        self.whbar_helper = None
        if config.whbar_helper_address:
            self.whbar_helper = rpc_client.w3.eth.contract(
                address=config.whbar_helper_address, abi=WHBAR_HELPER_ABI
            )

    def _deadline(self) -> int:
        return int(time.time()) + self.config.deadline_seconds

    def _apply_slippage(self, amount_out: int) -> int:
        return int(amount_out * (1 - self.config.slippage_tolerance))

    def quote_hbar_to_usdc(self, hbar_amount: float) -> float:
        """
        LET OP (23 aug 2026): gebruikt WHBAR's EIGEN 8-decimalen-conventie
        voor het amountIn-parameter, NIET de 18-decimalen-wei-conventie
        die voorheen werd gebruikt -- empirisch bevestigd tegen een echte
        pool (WHBAR/SAUCE, testnet): een quote met de 8-decimalen-aanname
        kwam binnen 0,5% van de verwachte prijsverhouding uit.
        """
        amount_in_raw = int(hbar_amount * (10 ** self.config.whbar_decimals))
        params = (
            self.config.whbar_address,
            self.config.usdc_address,
            amount_in_raw,
            self.config.fee_tier,
            0,  # sqrtPriceLimitX96 = 0 betekent geen limiet
        )
        result = self.quoter.functions.quoteExactInputSingle(params).call()
        amount_out = result[0]
        return amount_out / (10 ** self.config.usdc_decimals)

    def quote_usdc_to_hbar(self, usdc_amount: float) -> float:
        """LET OP (23 aug 2026): amountOut komt terug in WHBAR's eigen 8-decimalen-termen."""
        amount_in_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        params = (
            self.config.usdc_address,
            self.config.whbar_address,
            amount_in_raw,
            self.config.fee_tier,
            0,
        )
        result = self.quoter.functions.quoteExactInputSingle(params).call()
        amount_out = result[0]
        return amount_out / (10 ** self.config.whbar_decimals)

    def swap_hbar_to_usdc(self, hbar_amount: float) -> SwapResultV2:
        """
        LET OP (23 aug 2026): amountIn (het swap-parameter) en msg.value
        (de payable-waarde die daadwerkelijk verstuurd wordt) staan in
        VERSCHILLENDE conventies -- WHBAR's eigen 8 decimalen voor het
        eerste, de EVM-relay's 18-decimalen-wei-conventie voor het
        tweede. Voorheen werd dezelfde waarde voor beide hergebruikt,
        wat een factor 10^10-mismatch veroorzaakte.
        """
        estimated_usdc = self.quote_hbar_to_usdc(hbar_amount)
        amount_in_8dec = int(hbar_amount * (10 ** self.config.whbar_decimals))
        amount_in_wei_for_msg_value = self.rpc_client.w3.to_wei(hbar_amount, "ether")
        min_out_raw = self._apply_slippage(
            int(estimated_usdc * (10 ** self.config.usdc_decimals))
        )

        params = (
            self.config.whbar_address,
            self.config.usdc_address,
            self.config.fee_tier,
            self.rpc_client.address,
            self._deadline(),
            amount_in_8dec,
            min_out_raw,
            0,
        )

        swap_fn = self.router.functions.exactInputSingle(params)
        tx_hash = self.rpc_client.build_and_send_transaction(
            swap_fn, value_wei=amount_in_wei_for_msg_value
        )
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        return SwapResultV2(
            tx_hash=tx_hash,
            status=receipt["status"],
            amount_in=hbar_amount,
            estimated_amount_out=estimated_usdc,
            direction="HBAR_TO_USDC",
            fee_tier=self.config.fee_tier,
        )

    def swap_usdc_to_hbar(self, usdc_amount: float) -> SwapResultV2:
        """
        USDC -> HBAR. DEFINITIEF GECORRIGEERD (24 aug 2026, empirisch
        bevestigd op testnet): de SwapRouter's eigen unwrapWHBAR() werkt
        NIET voor WHBAR dat al in de eigen wallet zit -- die unwrapt
        alleen WHBAR dat de router ZELF binnen dezelfde transactie
        vasthoudt. De eerdere "fix" (recipient=router-adres) loste dit
        niet op en werd empirisch weerlegd (het bedrag bleef alsnog
        steken als WHBAR).

        De WERKELIJK correcte route, bevestigd via de officiele
        WhbarHelper-documentatie EN een geslaagde live-test: swap eerst
        gewoon naar de eigen wallet (recipient=gebruiker, zoals
        oorspronkelijk), en unwrap DAARNA in een aparte transactie via
        WhbarHelper.unwrapWhbar() -- dat vereist zelf weer een approve()
        vooraf, want WhbarHelper haalt de WHBAR actief op via
        safeTransferFrom(), i.p.v. dat je 'm zelf stuurt.

        Dit is dus TWEE (of drie, incl. approve) losse transacties,
        geen multicall-bundel meer.
        """
        self.ensure_usdc_approval(usdc_amount)

        estimated_hbar = self.quote_usdc_to_hbar(usdc_amount)
        amount_in_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        min_out_raw = self._apply_slippage(
            int(estimated_hbar * (10 ** self.config.whbar_decimals))
        )

        params = (
            self.config.usdc_address,
            self.config.whbar_address,
            self.config.fee_tier,
            self.rpc_client.address,  # terug naar het gebruikersadres -- correct
            self._deadline(),
            amount_in_raw,
            min_out_raw,
            0,
        )

        swap_fn = self.router.functions.exactInputSingle(params)
        tx_hash = self.rpc_client.build_and_send_transaction(swap_fn)
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        if receipt["status"] != "success" or not self.whbar_helper:
            return SwapResultV2(
                tx_hash=tx_hash, status=receipt["status"], amount_in=usdc_amount,
                estimated_amount_out=estimated_hbar, direction="USDC_TO_HBAR",
                fee_tier=self.config.fee_tier,
            )

        # Swap geslaagd -- nu de daadwerkelijk ontvangen WHBAR unwrappen.
        whbar_balance_raw = self.whbar_token.functions.balanceOf(self.rpc_client.address).call()
        if whbar_balance_raw == 0:
            return SwapResultV2(
                tx_hash=tx_hash, status=receipt["status"], amount_in=usdc_amount,
                estimated_amount_out=estimated_hbar, direction="USDC_TO_HBAR",
                fee_tier=self.config.fee_tier,
            )

        approve_fn = self.whbar_token.functions.approve(
            self.config.whbar_helper_address, whbar_balance_raw
        )
        approve_tx = self.rpc_client.build_and_send_transaction(approve_fn)
        self.rpc_client.wait_for_receipt(approve_tx)

        unwrap_fn = self.whbar_helper.functions.unwrapWhbar(whbar_balance_raw)
        unwrap_tx = self.rpc_client.build_and_send_transaction(unwrap_fn, gas_limit=1_000_000)
        self.rpc_client.wait_for_receipt(unwrap_tx)

        return SwapResultV2(
            tx_hash=tx_hash,
            status=receipt["status"],
            amount_in=usdc_amount,
            estimated_amount_out=estimated_hbar,
            direction="USDC_TO_HBAR",
            fee_tier=self.config.fee_tier,
        )

    def ensure_usdc_approval(self, usdc_amount: float):
        amount_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        approve_fn = self.usdc.functions.approve(self.config.swap_router_address, amount_raw)
        tx_hash = self.rpc_client.build_and_send_transaction(approve_fn)
        self.rpc_client.wait_for_receipt(tx_hash)

def build_swap_config_v2(network: str = "testnet", fee_tier: int = 3000,
                          slippage_tolerance: float = 0.01) -> SwapConfigV2:
    from config import (
        resolve_testnet_v2_addresses, resolve_mainnet_v2_addresses,
        resolve_testnet_addresses, resolve_mainnet_addresses,
    )

    if network == "testnet":
        v2 = resolve_testnet_v2_addresses()
        base = resolve_testnet_addresses()
    elif network == "mainnet":
        v2 = resolve_mainnet_v2_addresses()
        base = resolve_mainnet_addresses()
    else:
        raise ValueError(f"Onbekend netwerk: {network}")

    return SwapConfigV2(
        swap_router_address=v2.swap_router,
        quoter_v2_address=v2.quoter_v2,
        usdc_address=base.usdc,
        whbar_address=base.whbar_token,
        whbar_helper_address=base.whbar_helper,
        usdc_decimals=base.usdc_decimals,
        fee_tier=fee_tier,
        slippage_tolerance=slippage_tolerance,
    )
