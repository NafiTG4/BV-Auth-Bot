"""
pricing.py — BV Authenticator Subscription, Pricing & Payment Module
=====================================================================
এই একটা file এ সব আছে। Edit করতে হলে শুধু এই file এ করলেই হবে।

SETUP CHECKLIST:
  1. ALCHEMY_API_KEY set করো  (https://alchemy.com)
  2. HD_WALLET_MNEMONIC set করো  (fresh 24-word BIP39, NEVER reuse)
  3. PLANS dict edit করো — name, price, duration, features
  4. pip install bip_utils eth_account qrcode[pil] aiohttp base58
"""

from __future__ import annotations

import os, time, asyncio, logging, secrets
from io import BytesIO
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ContextTypes, CallbackQueryHandler

logger = logging.getLogger(__name__)

# Lazy imports — only loaded when subscription features are actually used
def _import_aiohttp():
    try:
        import aiohttp
        return aiohttp
    except ImportError:
        raise ImportError("Run: pip install aiohttp")

def _import_qrcode():
    try:
        import qrcode
        return qrcode
    except ImportError:
        raise ImportError("Run: pip install qrcode[pil]")

# ═════════════════════════════════════════════════════════════════════════════
# ① CONFIG — API KEYS
# ═════════════════════════════════════════════════════════════════════════════
ALCHEMY_API_KEY: str = os.environ.get("ALCHEMY_API_KEY", "YOUR_ALCHEMY_API_KEY_HERE")

# Alchemy base URLs per chain (update if you use a different network)
ALCHEMY_URLS: dict = {
    "ethereum": f"https://eth-mainnet.g.alchemy.com/v2/{{key}}",
    "base":     f"https://base-mainnet.g.alchemy.com/v2/{{key}}",
    "bnb":      f"https://bnb-mainnet.g.alchemy.com/v2/{{key}}",
    "polygon":  f"https://polygon-mainnet.g.alchemy.com/v2/{{key}}",
    "arbitrum": f"https://arb-mainnet.g.alchemy.com/v2/{{key}}",
    "optimism": f"https://opt-mainnet.g.alchemy.com/v2/{{key}}",
    "avalanche":f"https://avax-mainnet.g.alchemy.com/v2/{{key}}",
    "polkadot": f"https://moonbeam-mainnet.g.alchemy.com/v2/{{key}}",
}

# BIP39 mnemonic — generate fresh at https://iancoleman.io/bip39 (offline!)
HD_WALLET_MNEMONIC: str = os.environ.get(
    "HD_WALLET_MNEMONIC",
    "abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon about",
)

# ═════════════════════════════════════════════════════════════════════════════
# ② CONFIG — SUBSCRIPTION PLANS  ← edit here to change plans/prices
# ═════════════════════════════════════════════════════════════════════════════
# duration_days = 0  →  Lifetime (never expires)
# totp_limit    = None  →  inherits global bot default

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

# ═════════════════════════════════════════════════════════════════════════════
# ③ CONFIG — SUPPORTED TOKENS
# ═════════════════════════════════════════════════════════════════════════════
TOKENS: list[str] = ["USDC", "USDT"]

# ═════════════════════════════════════════════════════════════════════════════
# ④ CONFIG — SUPPORTED CHAINS  ← edit contract addresses if needed
# ═════════════════════════════════════════════════════════════════════════════
SUPPORTED_CHAINS: list[dict] = [
    {
        "id": "ethereum", "name": "Ethereum", "logo": "⟠",
        "family": "evm", "coin_type": 60, "bip44_index": 0,
        "token_contracts": {
            "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
            "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
        },
        "explorer_url": "https://etherscan.io",
    },
    {
        "id": "base", "name": "Base", "logo": "🔵",
        "family": "evm", "coin_type": 60, "bip44_index": 1,
        "token_contracts": {
            "USDC": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "USDT": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        },
        "explorer_url": "https://basescan.org",
    },
    {
        "id": "bnb", "name": "BNB Smart Chain", "logo": "🟡",
        "family": "evm", "coin_type": 60, "bip44_index": 2,
        "token_contracts": {
            "USDC": "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d",
            "USDT": "0x55d398326f99059fF775485246999027B3197955",
        },
        "explorer_url": "https://bscscan.com",
    },
    {
        "id": "polygon", "name": "Polygon PoS", "logo": "🟣",
        "family": "evm", "coin_type": 60, "bip44_index": 3,
        "token_contracts": {
            "USDC": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
            "USDT": "0xc2132D05D31c914a87C6611C10748AEb04B58e8F",
        },
        "explorer_url": "https://polygonscan.com",
    },
    {
        "id": "arbitrum", "name": "Arbitrum", "logo": "🔷",
        "family": "evm", "coin_type": 60, "bip44_index": 4,
        "token_contracts": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
        "explorer_url": "https://arbiscan.io",
    },
    {
        "id": "optimism", "name": "Optimism", "logo": "🔴",
        "family": "evm", "coin_type": 60, "bip44_index": 5,
        "token_contracts": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
        "explorer_url": "https://optimistic.etherscan.io",
    },
    {
        "id": "avalanche", "name": "Avalanche", "logo": "🏔",
        "family": "evm", "coin_type": 60, "bip44_index": 6,
        "token_contracts": {
            "USDC": "0xB97EF9Ef8734C71904D8002F8b6Bc66Dd9c48a6E",
            "USDT": "0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7",
        },
        "explorer_url": "https://snowtrace.io",
    },
    {
        "id": "solana", "name": "Solana", "logo": "◎",
        "family": "solana", "coin_type": 501, "bip44_index": 7,
        "token_contracts": {
            "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "USDT": "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
        },
        "explorer_url": "https://solscan.io",
    },
    {
        "id": "tron", "name": "Tron", "logo": "⚡",
        "family": "tron", "coin_type": 195, "bip44_index": 8,
        "token_contracts": {
            "USDC": "TEkxiTehnzSmSe2XqrBj4w32RUN966rdz8",
            "USDT": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
        },
        "explorer_url": "https://tronscan.org",
    },
    {
        "id": "sui", "name": "Sui", "logo": "💧",
        "family": "sui", "coin_type": 784, "bip44_index": 9,
        "token_contracts": {
            "USDC": "0x5d4b302506645c37ff133b98c4b50a4ae4614bb56fc4f8f7cf4a98e0b4783ade::coin::COIN",
            "USDT": "0xc060006111016b8a020ad5b33834984a437aaa7d3c74c18e09a95d48aceab08c::coin::COIN",
        },
        "explorer_url": "https://suiscan.xyz",
    },
    {
        "id": "polkadot", "name": "Polkadot (AssetHub)", "logo": "⚫",
        "family": "evm", "coin_type": 60, "bip44_index": 10,
        "token_contracts": {
            "USDC": "0x931715FEE2d06333043d11F658C8CE934aC61D0c",
            "USDT": "0xFFFFFFfFea09FB06d082fd1275CD48b191cbCD1d",
        },
        "explorer_url": "https://moonscan.io",
    },
]

# ═════════════════════════════════════════════════════════════════════════════
# ⑤ CONFIG — INVOICE SETTINGS
# ═════════════════════════════════════════════════════════════════════════════
INVOICE_EXPIRY_SECONDS: int   = 2 * 60 * 60   # 2 hours
REQUIRED_CONFIRMATIONS: int   = 1
INVOICE_POLL_INTERVAL:  int   = 60             # seconds between background checks
PAYMENT_TOLERANCE:      float = 0.99           # accept up to 1% underpayment

# ═════════════════════════════════════════════════════════════════════════════
# ⑥ HELPER LOOKUPS  (used internally — no need to edit)
# ═════════════════════════════════════════════════════════════════════════════
def get_plan(plan_id: str) -> dict | None:
    return PLANS.get(plan_id)

def get_chain(chain_id: str) -> dict | None:
    return next((c for c in SUPPORTED_CHAINS if c["id"] == chain_id), None)

def plans_list() -> list[dict]:
    return list(PLANS.values())

# ═════════════════════════════════════════════════════════════════════════════
# ⑦ DATABASE INIT  (called from bot.py's init_db)
# ═════════════════════════════════════════════════════════════════════════════
def init_subscription_db(c):
    """Create all subscription/payment tables. Pass open sqlite3 connection."""
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            vault_id     TEXT PRIMARY KEY,
            plan_id      TEXT NOT NULL,
            activated_at INTEGER NOT NULL,
            expires_at   INTEGER,
            is_active    INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payment_invoices (
            invoice_id    TEXT PRIMARY KEY,
            vault_id      TEXT NOT NULL,
            plan_id       TEXT NOT NULL,
            token         TEXT NOT NULL,
            chain_id      TEXT NOT NULL,
            address       TEXT NOT NULL,
            amount_usd    REAL NOT NULL,
            bip44_index   INTEGER NOT NULL,
            address_index INTEGER NOT NULL,
            status        TEXT DEFAULT 'pending',
            created_at    INTEGER NOT NULL,
            expires_at    INTEGER NOT NULL,
            paid_at       INTEGER,
            tx_hash       TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS used_addresses (
            address  TEXT PRIMARY KEY,
            vault_id TEXT NOT NULL,
            used_at  INTEGER NOT NULL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS chain_address_counter (
            chain_id   TEXT PRIMARY KEY,
            next_index INTEGER DEFAULT 0
        )
    """)

# ═════════════════════════════════════════════════════════════════════════════
# ⑧ SUBSCRIPTION DB HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def get_active_subscription(db_conn, vault_id: str) -> Optional[dict]:
    now = int(time.time())
    row = db_conn.execute(
        """SELECT * FROM subscriptions
           WHERE vault_id=? AND is_active=1
             AND (expires_at IS NULL OR expires_at > ?)""",
        (vault_id, now),
    ).fetchone()
    return dict(row) if row else None

def activate_subscription(db_conn, vault_id: str, plan_id: str):
    plan = get_plan(plan_id)
    now  = int(time.time())
    if plan["duration_days"] == 0:
        expires = None
    else:
        existing = get_active_subscription(db_conn, vault_id)
        base     = existing["expires_at"] if existing else now
        expires  = base + plan["duration_days"] * 86400
    db_conn.execute(
        """INSERT INTO subscriptions (vault_id, plan_id, activated_at, expires_at, is_active)
           VALUES (?,?,?,?,1)
           ON CONFLICT(vault_id) DO UPDATE SET
               plan_id=excluded.plan_id,
               activated_at=excluded.activated_at,
               expires_at=excluded.expires_at,
               is_active=1""",
        (vault_id, plan_id, now, expires),
    )
    db_conn.commit()

# ═════════════════════════════════════════════════════════════════════════════
# ⑨ HD WALLET ADDRESS GENERATION
# ═════════════════════════════════════════════════════════════════════════════
_SEED_CACHE: Optional[bytes] = None

def _import_wallet_libs():
    try:
        from bip_utils import Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes
        from eth_account import Account as EthAccount
        import base58
        return Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, EthAccount, base58
    except ImportError as e:
        raise ImportError(
            f"Missing wallet libs: {e}\n"
            "Run: pip install bip_utils eth_account base58"
        ) from e

def _get_seed() -> bytes:
    global _SEED_CACHE
    if _SEED_CACHE is None:
        Bip39SeedGenerator, *_ = _import_wallet_libs()
        _SEED_CACHE = Bip39SeedGenerator(HD_WALLET_MNEMONIC).Generate()
    return _SEED_CACHE

def _next_address_index(db_conn, chain_id: str) -> int:
    row = db_conn.execute(
        "SELECT next_index FROM chain_address_counter WHERE chain_id=?", (chain_id,)
    ).fetchone()
    idx = row["next_index"] if row else 0
    db_conn.execute(
        """INSERT INTO chain_address_counter (chain_id, next_index) VALUES (?,?)
           ON CONFLICT(chain_id) DO UPDATE SET next_index=excluded.next_index""",
        (chain_id, idx + 1),
    )
    db_conn.commit()
    return idx

def _derive_evm_address(address_index: int) -> str:
    Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, EthAccount, _ = _import_wallet_libs()
    seed    = _get_seed()
    account = (Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT)
               .AddressIndex(address_index))
    return EthAccount.from_key(account.PrivateKey().Raw().ToHex()).address

def _derive_solana_address(address_index: int) -> str:
    Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, _, __ = _import_wallet_libs()
    seed    = _get_seed()
    account = (Bip44.FromSeed(seed, Bip44Coins.SOLANA)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT)
               .AddressIndex(address_index))
    return account.PublicKey().ToAddress()

def _derive_tron_address(address_index: int) -> str:
    Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, _, __ = _import_wallet_libs()
    seed    = _get_seed()
    account = (Bip44.FromSeed(seed, Bip44Coins.TRON)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT)
               .AddressIndex(address_index))
    return account.PublicKey().ToAddress()

def _derive_sui_address(address_index: int) -> str:
    Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, _, __ = _import_wallet_libs()
    seed    = _get_seed()
    account = (Bip44.FromSeed(seed, Bip44Coins.SUI)
               .Purpose().Coin().Account(0)
               .Change(Bip44Changes.CHAIN_EXT)
               .AddressIndex(address_index))
    return account.PublicKey().ToAddress()

def generate_deposit_address(db_conn, chain: dict, vault_id: str) -> tuple[str, int]:
    """Return (address, address_index) — fresh and never reused."""
    family   = chain["family"]
    chain_id = chain["id"]
    while True:
        idx = _next_address_index(db_conn, chain_id)
        if family == "evm":
            addr = _derive_evm_address(idx)
        elif family == "solana":
            addr = _derive_solana_address(idx)
        elif family == "tron":
            addr = _derive_tron_address(idx)
        elif family == "sui":
            addr = _derive_sui_address(idx)
        else:
            addr = _derive_evm_address(idx)
        if not db_conn.execute("SELECT 1 FROM used_addresses WHERE address=?", (addr,)).fetchone():
            db_conn.execute(
                "INSERT INTO used_addresses (address, vault_id, used_at) VALUES (?,?,?)",
                (addr, vault_id, int(time.time())),
            )
            db_conn.commit()
            return addr, idx

# ═════════════════════════════════════════════════════════════════════════════
# ⑩ QR CODE GENERATION
# ═════════════════════════════════════════════════════════════════════════════
def generate_payment_qr(address: str, amount_usd: float,
                         token: str, chain: dict) -> BytesIO:
    contract     = chain["token_contracts"].get(token, "")
    amount_units = int(amount_usd * 1_000_000)
    family       = chain["family"]
    if family == "evm" and contract:
        uri = (f"ethereum:{contract}@/transfer"
               f"?address={address}&uint256={amount_units}")
    elif family == "tron":
        uri = f"tron:{address}?amount={amount_usd}&token={token}"
    elif family == "solana":
        uri = f"solana:{address}?amount={amount_usd}&spl-token={contract}"
    else:
        uri = address
    qrcode = _import_qrcode()
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8, border=3,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# ═════════════════════════════════════════════════════════════════════════════
# ⑪ INVOICE HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def create_invoice(db_conn, vault_id: str, plan_id: str,
                   token: str, chain: dict) -> dict:
    plan           = get_plan(plan_id)
    addr, addr_idx = generate_deposit_address(db_conn, chain, vault_id)
    now            = int(time.time())
    invoice_id     = secrets.token_hex(16)
    invoice = {
        "invoice_id":    invoice_id,
        "vault_id":      vault_id,
        "plan_id":       plan_id,
        "token":         token,
        "chain_id":      chain["id"],
        "address":       addr,
        "amount_usd":    plan["price_usd"],
        "bip44_index":   chain["bip44_index"],
        "address_index": addr_idx,
        "status":        "pending",
        "created_at":    now,
        "expires_at":    now + INVOICE_EXPIRY_SECONDS,
    }
    db_conn.execute(
        """INSERT INTO payment_invoices
           (invoice_id,vault_id,plan_id,token,chain_id,address,amount_usd,
            bip44_index,address_index,status,created_at,expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        list(invoice.values()),
    )
    db_conn.commit()
    return invoice

def get_pending_invoice(db_conn, vault_id: str) -> Optional[dict]:
    now = int(time.time())
    row = db_conn.execute(
        """SELECT * FROM payment_invoices
           WHERE vault_id=? AND status='pending' AND expires_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (vault_id, now),
    ).fetchone()
    return dict(row) if row else None

def expire_old_invoices(db_conn):
    now = int(time.time())
    db_conn.execute(
        "UPDATE payment_invoices SET status='expired' WHERE status='pending' AND expires_at <= ?",
        (now,),
    )
    db_conn.commit()

# ═════════════════════════════════════════════════════════════════════════════
# ⑫ PAYMENT VERIFICATION  (Alchemy for EVM, public RPCs for others)
# ═════════════════════════════════════════════════════════════════════════════
def _alchemy_url(chain_id: str) -> str:
    template = ALCHEMY_URLS.get(chain_id, "")
    return template.format(key=ALCHEMY_API_KEY)

async def _verify_evm(chain: dict, token: str, address: str,
                       amount_usd: float, since_ts: int) -> tuple[bool, str]:
    """Verify EVM token transfer using Alchemy alchemy_getAssetTransfers."""
    contract   = chain["token_contracts"].get(token)
    if not contract:
        return False, ""
    amount_min = amount_usd * PAYMENT_TOLERANCE
    url        = _alchemy_url(chain["id"])
    if not url:
        logger.warning(f"No Alchemy URL configured for chain {chain['id']}")
        return False, ""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "alchemy_getAssetTransfers",
        "params": [{
            "fromBlock":         "0x0",
            "toAddress":         address,
            "contractAddresses": [contract],
            "category":          ["erc20"],
            "withMetadata":      True,
            "excludeZeroValue":  True,
            "maxCount":          "0x19",
        }],
    }
    aiohttp = _import_aiohttp()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, json=payload) as r:
                data = await r.json()
            transfers = data.get("result", {}).get("transfers", [])
            for tx in transfers:
                metadata     = tx.get("metadata", {})
                block_ts_str = metadata.get("blockTimestamp", "")
                if block_ts_str:
                    import datetime as _dt
                    try:
                        block_ts = int(
                            _dt.datetime.fromisoformat(
                                block_ts_str.replace("Z", "+00:00")
                            ).timestamp()
                        )
                        if block_ts < since_ts:
                            continue
                    except Exception:
                        pass
                val = float(tx.get("value") or 0)
                if val >= amount_min:
                    return True, tx.get("hash", "")
        except Exception as e:
            logger.error(f"Alchemy EVM verify error ({chain['id']}): {e}")
    return False, ""

async def _verify_solana(token: str, address: str, amount_usd: float) -> tuple[bool, str]:
    mint       = get_chain("solana")["token_contracts"].get(token)
    if not mint:
        return False, ""
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    rpc        = "https://api.mainnet-beta.solana.com"
    aiohttp    = _import_aiohttp()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "getSignaturesForAddress",
                "params": [address, {"limit": 20}],
            }) as r:
                sigs = [s["signature"] for s in (await r.json()).get("result", [])]
            for sig in sigs:
                async with session.post(rpc, json={
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed",
                                     "maxSupportedTransactionVersion": 0}],
                }) as r:
                    tx = (await r.json()).get("result")
                if not tx:
                    continue
                for post in tx.get("meta", {}).get("postTokenBalances", []):
                    if post.get("mint") == mint and post.get("owner") == address:
                        if int(post.get("uiTokenAmount", {}).get("amount", "0")) >= amount_min:
                            return True, sig
        except Exception as e:
            logger.error(f"Solana verify error: {e}")
    return False, ""

async def _verify_tron(token: str, address: str,
                        amount_usd: float, since_ts: int) -> tuple[bool, str]:
    contract   = get_chain("tron")["token_contracts"].get(token)
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    url = (f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
           f"?contract_address={contract}&limit=20&only_to=true")
    aiohttp = _import_aiohttp()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as r:
                data = await r.json()
            for tx in data.get("data", []):
                if tx.get("block_timestamp", 0) // 1000 < since_ts:
                    continue
                if int(tx.get("value", "0")) >= amount_min:
                    return True, tx.get("transaction_id", "")
        except Exception as e:
            logger.error(f"Tron verify error: {e}")
    return False, ""

async def _verify_sui(token: str, address: str, amount_usd: float) -> tuple[bool, str]:
    coin_type  = get_chain("sui")["token_contracts"].get(token, "")
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    rpc        = "https://fullnode.mainnet.sui.io:443"
    aiohttp    = _import_aiohttp()
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(rpc, json={
                "jsonrpc": "2.0", "id": 1,
                "method": "suix_getCoins",
                "params": [address, coin_type, None, 10],
            }) as r:
                data = await r.json()
            total = sum(
                int(c.get("balance", 0))
                for c in data.get("result", {}).get("data", [])
            )
            if total >= amount_min:
                return True, "sui-balance-confirmed"
        except Exception as e:
            logger.error(f"Sui verify error: {e}")
    return False, ""

async def verify_payment(invoice: dict) -> tuple[bool, str]:
    chain = get_chain(invoice["chain_id"])
    if not chain:
        return False, ""
    family  = chain["family"]
    token   = invoice["token"]
    address = invoice["address"]
    amount  = invoice["amount_usd"]
    since   = invoice["created_at"]
    if family == "evm":
        return await _verify_evm(chain, token, address, amount, since)
    elif family == "solana":
        return await _verify_solana(token, address, amount)
    elif family == "tron":
        return await _verify_tron(token, address, amount, since)
    elif family == "sui":
        return await _verify_sui(token, address, amount)
    return False, ""

# ═════════════════════════════════════════════════════════════════════════════
# ⑬ BACKGROUND INVOICE POLLER
# ═════════════════════════════════════════════════════════════════════════════
async def _invoice_poller(db_getter, bot):
    while True:
        await asyncio.sleep(INVOICE_POLL_INTERVAL)
        try:
            with db_getter() as db:
                expire_old_invoices(db)
                rows = db.execute(
                    "SELECT * FROM payment_invoices WHERE status='pending'"
                ).fetchall()
            for row in rows:
                invoice = dict(row)
                paid, tx_hash = await verify_payment(invoice)
                if not paid:
                    continue
                with db_getter() as db:
                    db.execute(
                        """UPDATE payment_invoices
                           SET status='paid', paid_at=?, tx_hash=?
                           WHERE invoice_id=?""",
                        (int(time.time()), tx_hash, invoice["invoice_id"]),
                    )
                    activate_subscription(db, invoice["vault_id"], invoice["plan_id"])
                plan = get_plan(invoice["plan_id"])
                try:
                    with db_getter() as db:
                        user_row = db.execute(
                            "SELECT telegram_id FROM users WHERE vault_id=?",
                            (invoice["vault_id"],)
                        ).fetchone()
                    if user_row:
                        await bot.send_message(
                            chat_id=user_row["telegram_id"],
                            text=(
                                f"✅ Payment confirmed!\n\n"
                                f"Your *{plan['name']}* plan is now active.\n"
                                f"Thank you for subscribing! 🎉"
                            ),
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    logger.error(f"Poller notify error: {e}")
        except Exception as e:
            logger.error(f"Invoice poller error: {e}")

# ═════════════════════════════════════════════════════════════════════════════
# ⑭ KEYBOARD BUILDERS
# ═════════════════════════════════════════════════════════════════════════════
def _kb_plans() -> InlineKeyboardMarkup:
    buttons = []
    for p in PLANS.values():
        dur   = "Lifetime" if p["duration_days"] == 0 else f"{p['duration_days']}d"
        label = f"{p['name']}  —  ${p['price_usd']:.2f} / {dur}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"sub_plan:{p['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

def _kb_plan_detail(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 Buy Now", callback_data=f"sub_buy:{plan_id}")],
        [InlineKeyboardButton("⬅️ Back",    callback_data="sub_plans")],
    ])

def _kb_tokens(plan_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔵 USDC", callback_data=f"sub_token:{plan_id}:USDC"),
         InlineKeyboardButton("🟢 USDT", callback_data=f"sub_token:{plan_id}:USDT")],
        [InlineKeyboardButton("⬅️ Back", callback_data=f"sub_buy:{plan_id}")],
    ])

def _kb_chains(plan_id: str, token: str) -> InlineKeyboardMarkup:
    buttons = []
    row     = []
    for chain in SUPPORTED_CHAINS:
        row.append(InlineKeyboardButton(
            f"{chain['logo']} {chain['name']}",
            callback_data=f"sub_chain:{plan_id}:{token}:{chain['id']}",
        ))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"sub_token:{plan_id}:{token}")])
    return InlineKeyboardMarkup(buttons)

def _kb_invoice(invoice_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Check Payment Status", callback_data=f"sub_check:{invoice_id}")],
        [InlineKeyboardButton("❌ Cancel Invoice",        callback_data=f"sub_cancel:{invoice_id}")],
        [InlineKeyboardButton("🏠 Home",                  callback_data="main_menu")],
    ])

def _kb_paid() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Go Home", callback_data="main_menu")]])

# ═════════════════════════════════════════════════════════════════════════════
# ⑮ TELEGRAM CALLBACK HANDLERS
# ═════════════════════════════════════════════════════════════════════════════
async def cb_sub_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot import get_db, get_session
    q     = update.callback_query; await q.answer()
    uid   = update.effective_user.id
    vault = get_session(uid)
    sub_text = ""
    if vault:
        with get_db() as db:
            sub = get_active_subscription(db, vault)
        if sub:
            import datetime
            plan = get_plan(sub["plan_id"])
            if sub["expires_at"]:
                exp_str  = datetime.datetime.utcfromtimestamp(sub["expires_at"]).strftime("%d %b %Y")
                sub_text = f"✅ Active plan: *{plan['name']}* (expires {exp_str})\n\n"
            else:
                sub_text = f"✅ Active plan: *{plan['name']}* (Lifetime)\n\n"
    await q.edit_message_text(
        f"{sub_text}💎 *Choose a Subscription Plan*\n\nSelect a plan to see details.",
        parse_mode="Markdown",
        reply_markup=_kb_plans(),
    )

async def cb_sub_plan_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query; await q.answer()
    plan_id = q.data.split(":", 1)[1]
    plan    = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    dur_str  = "Lifetime (never expires)" if plan["duration_days"] == 0 else f"{plan['duration_days']} days"
    features = "\n".join(f"  ✔ {f}" for f in plan["features"])
    await q.edit_message_text(
        f"{plan['name']}\n\n"
        f"💰 Price: *${plan['price_usd']:.2f}* ({dur_str})\n\n"
        f"{plan['description']}\n\n"
        f"*What's included:*\n{features}",
        parse_mode="Markdown",
        reply_markup=_kb_plan_detail(plan_id),
    )

async def cb_sub_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query; await q.answer()
    plan_id = q.data.split(":", 1)[1]
    plan    = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    await q.edit_message_text(
        f"💳 *Pay for {plan['name']}*\n\nAmount: *${plan['price_usd']:.2f}*\n\n"
        "Select your preferred stablecoin:",
        parse_mode="Markdown",
        reply_markup=_kb_tokens(plan_id),
    )

async def cb_sub_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q               = update.callback_query; await q.answer()
    _, plan_id, token = q.data.split(":", 2)
    plan            = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    await q.edit_message_text(
        f"🌐 *Select Network*\n\n"
        f"Plan: *{plan['name']}* — ${plan['price_usd']:.2f} {token}\n\n"
        "Choose the blockchain network you will send from:",
        parse_mode="Markdown",
        reply_markup=_kb_chains(plan_id, token),
    )

async def cb_sub_chain(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot import get_db, get_session
    q                         = update.callback_query; await q.answer()
    _, plan_id, token, chain_id = q.data.split(":", 3)
    uid                       = update.effective_user.id
    vault                     = get_session(uid)
    if not vault:
        await q.answer("Please log in first.", show_alert=True); return
    plan  = get_plan(plan_id)
    chain = get_chain(chain_id)
    if not plan or not chain:
        await q.answer("Invalid plan or chain.", show_alert=True); return
    with get_db() as db:
        if get_pending_invoice(db, vault):
            await q.answer(
                "You already have a pending invoice. Cancel it first.",
                show_alert=True,
            ); return
    await q.edit_message_text("⏳ Generating your deposit address...")
    try:
        with get_db() as db:
            invoice = await asyncio.to_thread(create_invoice, db, vault, plan_id, token, chain)
    except Exception as e:
        logger.error(f"Invoice creation error: {e}")
        await q.edit_message_text(
            "❌ Failed to generate address. Please try again.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="sub_plans")
            ]]),
        ); return
    import datetime
    exp_str = datetime.datetime.utcfromtimestamp(invoice["expires_at"]).strftime("%d %b %Y %H:%M UTC")
    qr_buf  = await asyncio.to_thread(
        generate_payment_qr,
        invoice["address"], invoice["amount_usd"], token, chain,
    )
    caption = (
        f"📦 *{plan['name']}* — ${plan['price_usd']:.2f} {token}\n"
        f"🌐 Network: *{chain['logo']} {chain['name']}*\n\n"
        f"📬 *Send exactly:* `{plan['price_usd']:.2f}` {token}\n"
        f"📮 *To address:*\n`{invoice['address']}`\n\n"
        f"⏳ Invoice expires: {exp_str}\n"
        f"🆔 Invoice ID: `{invoice['invoice_id'][:12]}…`\n\n"
        "Scan the QR with any wallet. After sending, tap *Check Payment Status*."
    )
    try:
        await q.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_photo(
        photo=InputFile(qr_buf, filename="payment_qr.png"),
        caption=caption,
        parse_mode="Markdown",
        reply_markup=_kb_invoice(invoice["invoice_id"]),
    )

async def cb_sub_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot import get_db
    q          = update.callback_query; await q.answer("Checking…")
    invoice_id = q.data.split(":", 1)[1]
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM payment_invoices WHERE invoice_id=?", (invoice_id,)
        ).fetchone()
    if not row:
        await q.answer("Invoice not found.", show_alert=True); return
    invoice = dict(row)
    now     = int(time.time())
    if invoice["status"] == "paid":
        plan = get_plan(invoice["plan_id"])
        await q.edit_message_caption(
            caption=f"✅ *Payment confirmed!*\n\nPlan *{plan['name']}* is now active.\nTx: `{invoice['tx_hash'] or 'confirmed'}`",
            parse_mode="Markdown", reply_markup=_kb_paid(),
        ); return
    if invoice["status"] == "expired" or invoice["expires_at"] <= now:
        with get_db() as db:
            db.execute("UPDATE payment_invoices SET status='expired' WHERE invoice_id=?", (invoice_id,))
            db.commit()
        await q.edit_message_caption(
            caption="⌛ This invoice has *expired*. Please start a new payment.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 New Payment", callback_data="sub_plans"),
                InlineKeyboardButton("🏠 Home",        callback_data="main_menu"),
            ]]),
        ); return
    paid, tx_hash = await verify_payment(invoice)
    if paid:
        with get_db() as db:
            db.execute(
                "UPDATE payment_invoices SET status='paid', paid_at=?, tx_hash=? WHERE invoice_id=?",
                (now, tx_hash, invoice_id),
            )
            activate_subscription(db, invoice["vault_id"], invoice["plan_id"])
        plan = get_plan(invoice["plan_id"])
        await q.edit_message_caption(
            caption=f"✅ *Payment confirmed!*\n\nYour *{plan['name']}* plan is now active. 🎉\nTx: `{tx_hash or 'confirmed'}`",
            parse_mode="Markdown", reply_markup=_kb_paid(),
        )
    else:
        remaining = invoice["expires_at"] - now
        chain     = get_chain(invoice["chain_id"])
        plan      = get_plan(invoice["plan_id"])
        await q.edit_message_caption(
            caption=(
                f"⏳ *Payment Pending*\n\n"
                f"Plan: *{plan['name']}*\n"
                f"Amount: `{invoice['amount_usd']:.2f}` {invoice['token']}\n"
                f"Network: {chain['logo']} {chain['name']}\n"
                f"Address: `{invoice['address']}`\n\n"
                f"⌛ Expires in: {remaining // 60} min\n\n"
                "Payment not detected yet. Wait for confirmation then check again."
            ),
            parse_mode="Markdown",
            reply_markup=_kb_invoice(invoice_id),
        )

async def cb_sub_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot import get_db
    q          = update.callback_query; await q.answer()
    invoice_id = q.data.split(":", 1)[1]
    with get_db() as db:
        db.execute(
            "UPDATE payment_invoices SET status='expired' WHERE invoice_id=? AND status='pending'",
            (invoice_id,),
        )
        db.commit()
    await q.edit_message_caption(
        caption="❌ Invoice cancelled. You can start a new payment anytime.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 New Payment", callback_data="sub_plans"),
            InlineKeyboardButton("🏠 Home",        callback_data="main_menu"),
        ]]),
    )

# ═════════════════════════════════════════════════════════════════════════════
# ⑯ REGISTER HANDLERS  (call this once at bot startup)
# ═════════════════════════════════════════════════════════════════════════════
def register_subscription_handlers(app, db_getter):
    app.add_handler(CallbackQueryHandler(cb_sub_plans,       pattern=r"^sub_plans$"))
    app.add_handler(CallbackQueryHandler(cb_sub_plan_detail, pattern=r"^sub_plan:"))
    app.add_handler(CallbackQueryHandler(cb_sub_buy,         pattern=r"^sub_buy:"))
    app.add_handler(CallbackQueryHandler(cb_sub_token,       pattern=r"^sub_token:"))
    app.add_handler(CallbackQueryHandler(cb_sub_chain,       pattern=r"^sub_chain:"))
    app.add_handler(CallbackQueryHandler(cb_sub_check,       pattern=r"^sub_check:"))
    app.add_handler(CallbackQueryHandler(cb_sub_cancel,      pattern=r"^sub_cancel:"))

    async def _start_poller(application):
        asyncio.create_task(_invoice_poller(db_getter, application.bot))

    app.post_init = _start_poller
    logger.info("Subscription handlers registered.")
