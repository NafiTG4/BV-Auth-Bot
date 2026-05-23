"""
pricing.py — BV Authenticator Subscription & Payment Configuration
===================================================================
Edit this file to update plans, prices, chains, and API keys.
All monetary amounts are in USD (USDC/USDT equivalent).

SETUP CHECKLIST:
  1. Set MORALIS_API_KEY  (https://moralis.io — free tier: 40k req/day)
  2. Set HD_WALLET_MNEMONIC  (generate a fresh 24-word BIP39 mnemonic, NEVER reuse)
  3. Edit PLANS dict — name, price, duration, features
  4. Adjust SUPPORTED_CHAINS if needed
"""

import os

# ─────────────────────────────────────────────────────────────────────────────
# API KEYS  (set via environment variables or edit directly here)
# ─────────────────────────────────────────────────────────────────────────────
MORALIS_API_KEY: str = os.environ.get("MORALIS_API_KEY", "YOUR_MORALIS_API_KEY_HERE")

# BIP39 mnemonic for HD wallet derivation.
# IMPORTANT: Generate a fresh one, fund it only to collect payments,
# and NEVER share it. Store in env var in production.
HD_WALLET_MNEMONIC: str = os.environ.get(
    "HD_WALLET_MNEMONIC",
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
)

# ─────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTION PLANS
# ─────────────────────────────────────────────────────────────────────────────
# duration_days: number of days the plan is valid. Use 0 for lifetime.
# totp_limit: max TOTP entries. Use None to inherit the global bot default.
# features: list of strings shown to user in plan detail screen.

PLANS: dict = {
    "starter": {
        "id":            "starter",
        "name":          "⚡ Starter",
        "price_usd":     4.99,
        "duration_days": 30,
        "totp_limit":    None,
        "features": [
            "Full TOTP vault access",
            "Export & Import vault",
            "Offline auto-backup",
            "Email support",
        ],
        "description": "Perfect for personal use.",
    },
    "pro": {
        "id":            "pro",
        "name":          "🚀 Pro",
        "price_usd":     9.99,
        "duration_days": 30,
        "totp_limit":    500,
        "features": [
            "Everything in Starter",
            "500 TOTP entries",
            "Priority support",
            "Advanced security features",
        ],
        "description": "For power users who need more.",
    },
    "lifetime": {
        "id":            "lifetime",
        "name":          "👑 Lifetime",
        "price_usd":     49.99,
        "duration_days": 0,
        "totp_limit":    1000,
        "features": [
            "Everything in Pro",
            "1000 TOTP entries",
            "Lifetime access — pay once",
            "All future features included",
            "VIP support",
        ],
        "description": "One-time payment, forever access.",
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# SUPPORTED TOKENS
# ─────────────────────────────────────────────────────────────────────────────
TOKENS: list[str] = ["USDC", "USDT"]

# ─────────────────────────────────────────────────────────────────────────────
# SUPPORTED CHAINS
# ─────────────────────────────────────────────────────────────────────────────
SUPPORTED_CHAINS: list[dict] = [
    {
        "id": "ethereum", "name": "Ethereum", "logo": "⟠",
        "family": "evm", "coin_type": 60, "bip44_index": 0,
        "moralis_chain": "eth",
        "token_contracts": {
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        },
        "explorer_url": "https://etherscan.io",
    },
    {
        "id": "base", "name": "Base", "logo": "🔵",
        "family": "evm", "coin_type": 60, "bip44_index": 1,
        "moralis_chain": "base",
        "token_contracts": {
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        },
        "explorer_url": "https://basescan.org",
    },
    {
        "id": "bnb", "name": "BNB Smart Chain", "logo": "🟡",
        "family": "evm", "coin_type": 60, "bip44_index": 2,
        "moralis_chain": "bsc",
        "token_contracts": {
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
        },
        "explorer_url": "https://bscscan.com",
    },
    {
        "id": "polygon", "name": "Polygon PoS", "logo": "🟣",
        "family": "evm", "coin_type": 60, "bip44_index": 3,
        "moralis_chain": "polygon",
        "token_contracts": {
            "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        },
        "explorer_url": "https://polygonscan.com",
    },
    {
        "id": "arbitrum", "name": "Arbitrum", "logo": "🔷",
        "family": "evm", "coin_type": 60, "bip44_index": 4,
        "moralis_chain": "arbitrum",
        "token_contracts": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
        "explorer_url": "https://arbiscan.io",
    },
    {
        "id": "optimism", "name": "Optimism", "logo": "🔴",
        "family": "evm", "coin_type": 60, "bip44_index": 5,
        "moralis_chain": "optimism",
        "token_contracts": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
        "explorer_url": "https://optimistic.etherscan.io",
    },
    {
        "id": "avalanche", "name": "Avalanche", "logo": "🏔",
        "family": "evm", "coin_type": 60, "bip44_index": 6,
        "moralis_chain": "avalanche",
        "token_contracts": {
            "USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
            "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        },
        "explorer_url": "https://snowtrace.io",
    },
    {
        "id": "solana", "name": "Solana", "logo": "◎",
        "family": "solana", "coin_type": 501, "bip44_index": 7,
        "moralis_chain": None,
        "token_contracts": {
            "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        },
        "explorer_url": "https://solscan.io",
    },
    {
        "id": "tron", "name": "Tron", "logo": "⚡",
        "family": "tron", "coin_type": 195, "bip44_index": 8,
        "moralis_chain": None,
        "token_contracts": {
            "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
            "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        },
        "explorer_url": "https://tronscan.org",
    },
    {
        "id": "sui", "name": "Sui", "logo": "💧",
        "family": "sui", "coin_type": 784, "bip44_index": 9,
        "moralis_chain": None,
        "token_contracts": {
            "USDC": "0x5d4b302506645c37ff133b98c4b50a4ae4614bb56fc4f8f7cf4a98e0b4783ade::coin::COIN",
            "USDT": "0xc060006111016b8a020ad5b33834984a437aaa7d3c74c18e09a95d48aceab08c::coin::COIN",
        },
        "explorer_url": "https://suiscan.xyz",
    },
    {
        "id": "polkadot", "name": "Polkadot (AssetHub)", "logo": "⚫",
        "family": "evm", "coin_type": 60, "bip44_index": 10,
        "moralis_chain": "moonbeam",
        "token_contracts": {
            "USDC": "0x931715FEE2d06333043d11F658C8CE934aC61D0c",
            "USDT": "0xFFFFFFfFea09FB06d082fd1275CD48b191cbCD1d",
        },
        "explorer_url": "https://moonscan.io",
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# INVOICE SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
INVOICE_EXPIRY_SECONDS: int  = 2 * 60 * 60   # 2 hours
REQUIRED_CONFIRMATIONS: int  = 1
INVOICE_POLL_INTERVAL:  int  = 60             # seconds between background checks
PAYMENT_TOLERANCE:      float = 0.99          # accept up to 1% underpayment

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def get_plan(plan_id: str) -> dict | None:
    return PLANS.get(plan_id)

def get_chain(chain_id: str) -> dict | None:
    return next((c for c in SUPPORTED_CHAINS if c["id"] == chain_id), None)

def plans_list() -> list[dict]:
    return list(PLANS.values())
