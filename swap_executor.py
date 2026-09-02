"""
swap_executor.py

Voert HBAR <-> USDC swaps uit via het SaucerSwap V1 Router-contract
(UniswapV2Router02, met HBAR als de "ETH"-equivalent en WHBAR als wrapper).

Het ABI-fragment hieronder is direct afgeleid van de officiele interface
in github.com/saucerswaplabs/saucerswap-periphery
(contracts/interfaces/IUniswapV2Router02.sol) -- NIET uit geheugen
gereconstrueerd, om het risico op een foutief ABI te vermijden.

BELANGRIJK -- nog in te vullen voordat dit bruikbaar is:
- ROUTER_ADDRESS: het EVM-adres (0x...) van SaucerSwap's V1 Router op
  mainnet. Haal dit op van https://docs.saucerswap.finance/ en verifieer
  het adres op HashScan (https://hashscan.io/) voordat je hiermee handelt.
- USDC_ADDRESS / WHBAR_ADDRESS: idem, EVM-adressen van de tokens.
- Voor een nieuw HTS-token (zoals USDC) moet het bot-account het token
  eerst "associaten" voordat het kan ontvangen/versturen -- dat is een
  aparte Hedera-specifieke stap, los van de swap zelf. Zonder associatie
  faalt de swap.
"""

import time
from dataclasses import dataclass
from typing import List, Optional

from hedera_rpc_client import HederaRpcClient
from config import resolve_testnet_addresses, resolve_mainnet_addresses, HEDERA_NETWORK


# Minimaal ABI-fragment, alleen de functies die deze bot nodig heeft.
# Signaturen 1-op-1 overgenomen uit IUniswapV2Router02.sol.
ROUTER_ABI = [
    {
        "name": "swapExactETHForTokens",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "swapExactTokensForETH",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "amountOutMin", "type": "uint256"},
            {"name": "path", "type": "address[]"},
            {"name": "to", "type": "address"},
            {"name": "deadline", "type": "uint256"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "getAmountsOut",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "amountIn", "type": "uint256"},
            {"name": "path", "type": "address[]"},
        ],
        "outputs": [{"name": "amounts", "type": "uint256[]"}],
    },
    {
        "name": "whbar",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
]

# Standaard ERC20/HTS balanceOf + approve, nodig voor de USDC-kant van de swap
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
class SwapConfig:
    router_address: str      # EVM-adres SaucerSwap V1 Router -- MOET je zelf invullen
    usdc_address: str        # EVM-adres USDC token op Hedera -- MOET je zelf invullen
    whbar_address: str       # EVM-adres WHBAR token -- MOET je zelf invullen
    usdc_decimals: int = 6
    slippage_tolerance: float = 0.01  # 1%
    deadline_seconds: int = 120


@dataclass
class SwapResult:
    tx_hash: str
    status: str
    amount_in: float
    estimated_amount_out: float
    direction: str


class SwapExecutor:
    def __init__(self, rpc_client: HederaRpcClient, config: SwapConfig):
        self.rpc_client = rpc_client
        self.config = config
        self.router = rpc_client.w3.eth.contract(
            address=config.router_address, abi=ROUTER_ABI
        )
        self.usdc = rpc_client.w3.eth.contract(
            address=config.usdc_address, abi=ERC20_ABI
        )

    def verify_whbar_address(self) -> bool:
        """
        Leest het WHBAR-adres rechtstreeks van de Router (on-chain, read-only,
        geen transactie/kosten) en vergelijkt het met config.whbar_address.
        Roep dit aan voordat je een eerste live swap probeert.
        """
        onchain_whbar = self.router.functions.whbar().call()
        matches = onchain_whbar.lower() == self.config.whbar_address.lower()
        if not matches:
            print(
                f"WAARSCHUWING: Router's whbar()-adres ({onchain_whbar}) komt niet "
                f"overeen met config.whbar_address ({self.config.whbar_address}). "
                f"Swaps zullen falen met INVALID_PATH -- niet doorgaan."
            )
        return matches

    def _deadline(self) -> int:
        return int(time.time()) + self.config.deadline_seconds

    def _apply_slippage(self, amount_out: int) -> int:
        return int(amount_out * (1 - self.config.slippage_tolerance))

    def quote_hbar_to_usdc(self, hbar_amount: float) -> float:
        """Geeft de verwachte USDC-output voor een gegeven HBAR-input."""
        amount_in_wei = self.rpc_client.w3.to_wei(hbar_amount, "ether")
        path = [self.config.whbar_address, self.config.usdc_address]
        amounts = self.router.functions.getAmountsOut(amount_in_wei, path).call()
        return amounts[-1] / (10 ** self.config.usdc_decimals)

    def quote_usdc_to_hbar(self, usdc_amount: float) -> float:
        """Geeft de verwachte HBAR-output voor een gegeven USDC-input."""
        amount_in_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        path = [self.config.usdc_address, self.config.whbar_address]
        amounts = self.router.functions.getAmountsOut(amount_in_raw, path).call()
        return self.rpc_client.w3.from_wei(amounts[-1], "ether")

    def swap_hbar_to_usdc(self, hbar_amount: float) -> SwapResult:
        """
        Swapt HBAR naar USDC. hbar_amount is in whole HBAR (bv. 100.0).
        """
        estimated_usdc = self.quote_hbar_to_usdc(hbar_amount)
        amount_in_wei = self.rpc_client.w3.to_wei(hbar_amount, "ether")
        min_out_raw = self._apply_slippage(
            int(estimated_usdc * (10 ** self.config.usdc_decimals))
        )

        path = [self.config.whbar_address, self.config.usdc_address]

        swap_fn = self.router.functions.swapExactETHForTokens(
            min_out_raw, path, self.rpc_client.address, self._deadline()
        )

        tx = swap_fn.build_transaction({"value": amount_in_wei})
        # build_and_send_transaction verwacht een 'contract_function' object,
        # maar omdat swapExactETHForTokens payable is (msg.value nodig),
        # bouwen we de tx hier direct op met de juiste 'value'.
        tx_hash = self.rpc_client.build_and_send_transaction(swap_fn, value_wei=amount_in_wei)
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        return SwapResult(
            tx_hash=tx_hash,
            status=receipt["status"],
            amount_in=hbar_amount,
            estimated_amount_out=estimated_usdc,
            direction="HBAR_TO_USDC",
        )

    def swap_usdc_to_hbar(self, usdc_amount: float) -> SwapResult:
        """
        Swapt USDC naar HBAR. Vereist vooraf een approve() -- zie
        ensure_usdc_approval(). usdc_amount is in whole USDC (bv. 50.0).
        """
        self.ensure_usdc_approval(usdc_amount)

        estimated_hbar = self.quote_usdc_to_hbar(usdc_amount)
        amount_in_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        min_out_wei = self._apply_slippage(
            self.rpc_client.w3.to_wei(estimated_hbar, "ether")
        )

        path = [self.config.usdc_address, self.config.whbar_address]

        swap_fn = self.router.functions.swapExactTokensForETH(
            amount_in_raw, min_out_wei, path, self.rpc_client.address, self._deadline()
        )

        tx_hash = self.rpc_client.build_and_send_transaction(swap_fn)
        receipt = self.rpc_client.wait_for_receipt(tx_hash)

        return SwapResult(
            tx_hash=tx_hash,
            status=receipt["status"],
            amount_in=usdc_amount,
            estimated_amount_out=estimated_hbar,
            direction="USDC_TO_HBAR",
        )

    def ensure_usdc_approval(self, usdc_amount: float):
        """Approve de Router om USDC namens het bot-account te spenderen."""
        amount_raw = int(usdc_amount * (10 ** self.config.usdc_decimals))
        approve_fn = self.usdc.functions.approve(self.config.router_address, amount_raw)
        tx_hash = self.rpc_client.build_and_send_transaction(approve_fn)
        self.rpc_client.wait_for_receipt(tx_hash)


def build_swap_config(network: str = None, slippage_tolerance: float = 0.01) -> SwapConfig:
    """
    Bouwt een SwapConfig direct op uit config.py, zodat je nooit handmatig
    adressen hoeft over te typen. network: 'testnet' of 'mainnet', valt
    terug op config.HEDERA_NETWORK als niet opgegeven.
    """
    network = network or HEDERA_NETWORK
    if network == "testnet":
        addresses = resolve_testnet_addresses()
    elif network == "mainnet":
        addresses = resolve_mainnet_addresses()
    else:
        raise ValueError(f"Onbekend netwerk: {network}")

    return SwapConfig(
        router_address=addresses.router,
        usdc_address=addresses.usdc,
        whbar_address=addresses.whbar_token,
        usdc_decimals=addresses.usdc_decimals,
        slippage_tolerance=slippage_tolerance,
    )
