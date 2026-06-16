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

def get_alchemy_api_key() -> str:
    """Return Alchemy API key from DB (admin-set) or fall back to env/default."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key='alchemy_api_key'"
            ).fetchone()
        if row and row["value"]:
            return row["value"]
    except Exception:
        pass
    return ALCHEMY_API_KEY

# ── Plan price helpers ─────────────────────────────────────────────────────────
_DEFAULT_PRICES = {
    "plus_30":   1.19,
    "plus_year": 9.99,
    "pro_30":    2.49,
    "pro_year":  14.90,
}

def get_plan_price(plan_id: str) -> float:
    """Return admin-set price from DB or default."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key=?",
                (f"plan_price_{plan_id}",)
            ).fetchone()
        if row and row["value"]:
            return float(row["value"])
    except Exception:
        pass
    return _DEFAULT_PRICES.get(plan_id, PLANS.get(plan_id, {}).get("price_usd", 0))

def get_plan_with_price(plan_id: str) -> dict:
    """Return plan dict with current DB price applied."""
    plan = dict(PLANS.get(plan_id, {}))
    plan["price_usd"] = get_plan_price(plan_id)
    return plan

# ── Stablecoin / Network toggle helpers ───────────────────────────────────────
def is_token_enabled(plan_group: str, token: str) -> bool:
    """Returns True if the token (USDC/USDT) is enabled for this plan group."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key=?",
                (f"{plan_group}_{token.lower()}_enabled",)
            ).fetchone()
        if row and row["value"] is not None:
            return row["value"] not in ("0", "false", "False", False, 0)
    except Exception:
        pass
    return True  # default: enabled

def is_network_enabled(plan_group: str, chain_id: str) -> bool:
    """Returns True if the network is enabled for this plan group."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key=?",
                (f"{plan_group}_network_{chain_id}",)
            ).fetchone()
        if row and row["value"] is not None:
            return row["value"] not in ("0", "false", "False", False, 0)
    except Exception:
        pass
    return True  # default: enabled

def is_global_detect_enabled(plan_group: str) -> bool:
    """Returns True if global QR/secret detection is enabled for this plan group."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key=?",
                (f"{plan_group}_global_detect",)
            ).fetchone()
        if row and row["value"] is not None:
            return row["value"] not in ("0", "false", "False", False, 0)
    except Exception:
        pass
    return True  # default: enabled

def get_share_limit(plan_group: str) -> int:
    """Returns daily share limit for a plan group. Default: 1."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT value FROM bot_settings WHERE key=?",
                (f"{plan_group}_share_limit",)
            ).fetchone()
        if row and row["value"]:
            return int(row["value"])
    except Exception:
        pass
    return 1  # default: 1 share per day

# Alchemy base URLs per chain (update if you use a different network)
ALCHEMY_URLS: dict = {
    "base":     f"https://base-mainnet.g.alchemy.com/v2/{{key}}",
    "bnb":      f"https://bnb-mainnet.g.alchemy.com/v2/{{key}}",
    "polygon":  f"https://polygon-mainnet.g.alchemy.com/v2/{{key}}",
    "arbitrum": f"https://arb-mainnet.g.alchemy.com/v2/{{key}}",
    "optimism": f"https://opt-mainnet.g.alchemy.com/v2/{{key}}",
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
    "basic": {
        "id":            "basic",
        "plan_group":    "basic",
        "name":          "Basic",
        "price_usd":     0,
        "duration_days": 0,
        "totp_limit":    None,
    },
    "plus_30": {
        "id":            "plus_30",
        "plan_group":    "plus",
        "name":          "Plus",
        "price_usd":     1.19,
        "duration_days": 30,
        "totp_limit":    None,
    },
    "plus_year": {
        "id":            "plus_year",
        "plan_group":    "plus",
        "name":          "Plus",
        "price_usd":     9.99,
        "duration_days": 365,
        "totp_limit":    None,
    },
    "pro_30": {
        "id":            "pro_30",
        "plan_group":    "pro",
        "name":          "Pro",
        "price_usd":     2.49,
        "duration_days": 30,
        "totp_limit":    None,
    },
    "pro_year": {
        "id":            "pro_year",
        "plan_group":    "pro",
        "name":          "Pro",
        "price_usd":     14.90,
        "duration_days": 365,
        "totp_limit":    None,
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
        "id": "arbitrum", "name": "Arbitrum", "logo": "🔷",
        "family": "evm", "coin_type": 60, "bip44_index": 4,
        "token_contracts": {
            "USDC": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
            "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
        },
        "explorer_url": "https://arbiscan.io",
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
        "id": "optimism", "name": "Optimism", "logo": "🔴",
        "family": "evm", "coin_type": 60, "bip44_index": 5,
        "token_contracts": {
            "USDC": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
            "USDT": "0x94b008aA00579c1307B0EF2c499aD98a8ce58e58",
        },
        "explorer_url": "https://optimistic.etherscan.io",
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
    p = PLANS.get(plan_id)
    if p is None:
        return None
    result = dict(p)
    result["price_usd"] = get_plan_price(plan_id)
    return result

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
            vault_id        TEXT PRIMARY KEY,
            plan_id         TEXT NOT NULL,
            activated_at    INTEGER NOT NULL,
            expires_at      INTEGER,
            is_active       INTEGER DEFAULT 1,
            pending_plan_id TEXT
        )
    """)
    # Migration: add pending_plan_id if upgrading from an older schema
    try:
        cols = [r[1] for r in c.execute("PRAGMA table_info(subscriptions)").fetchall()]
        if "pending_plan_id" not in cols:
            c.execute("ALTER TABLE subscriptions ADD COLUMN pending_plan_id TEXT")
    except Exception:
        pass
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
    if row:
        return dict(row)
    # Current plan may have just expired with a pending plan queued — promote it
    raw = db_conn.execute("SELECT * FROM subscriptions WHERE vault_id=?", (vault_id,)).fetchone()
    if raw and raw["pending_plan_id"]:
        activate_pending_plan_if_expired(db_conn, vault_id)
        row = db_conn.execute(
            """SELECT * FROM subscriptions
               WHERE vault_id=? AND is_active=1
                 AND (expires_at IS NULL OR expires_at > ?)""",
            (vault_id, now),
        ).fetchone()
        return dict(row) if row else None
    return None

def activate_subscription(db_conn, vault_id: str, plan_id: str):
    """
    Activate a plan for a vault.
    - If no active subscription exists: activate immediately.
    - If an active subscription with the SAME plan_id exists: extend its expiry.
    - If an active subscription with a DIFFERENT plan_id exists: queue this plan
      as pending_plan_id; it will auto-activate once the current one expires.
    """
    plan = get_plan(plan_id)
    now  = int(time.time())
    existing = get_active_subscription(db_conn, vault_id)

    if not existing:
        # No active plan — activate immediately
        expires = None if plan["duration_days"] == 0 else now + plan["duration_days"] * 86400
        db_conn.execute(
            """INSERT INTO subscriptions (vault_id, plan_id, activated_at, expires_at, is_active, pending_plan_id)
               VALUES (?,?,?,?,1,NULL)
               ON CONFLICT(vault_id) DO UPDATE SET
                   plan_id=excluded.plan_id,
                   activated_at=excluded.activated_at,
                   expires_at=excluded.expires_at,
                   is_active=1,
                   pending_plan_id=NULL""",
            (vault_id, plan_id, now, expires),
        )
        db_conn.commit()
        return

    if existing["plan_id"] == plan_id:
        # Same plan — extend from current expiry
        if plan["duration_days"] == 0:
            expires = None
        else:
            base    = existing["expires_at"] if existing["expires_at"] else now
            expires = base + plan["duration_days"] * 86400
        db_conn.execute(
            "UPDATE subscriptions SET expires_at=? WHERE vault_id=?",
            (expires, vault_id),
        )
        db_conn.commit()
        return

    # Different plan — queue it; it activates automatically once current plan expires
    db_conn.execute(
        "UPDATE subscriptions SET pending_plan_id=? WHERE vault_id=?",
        (plan_id, vault_id),
    )
    db_conn.commit()

def activate_pending_plan_if_expired(db_conn, vault_id: str):
    """
    Check if the vault's active subscription has expired and a plan is queued.
    If so, activate the queued plan starting now. Called by the background poller
    and on-demand whenever the subscription is read.
    """
    now = int(time.time())
    row = db_conn.execute(
        "SELECT * FROM subscriptions WHERE vault_id=?", (vault_id,)
    ).fetchone()
    if not row:
        return
    row = dict(row)
    expired = row["expires_at"] is not None and row["expires_at"] <= now
    if expired and row.get("pending_plan_id"):
        next_plan_id = row["pending_plan_id"]
        next_plan    = get_plan(next_plan_id)
        expires = None if next_plan["duration_days"] == 0 else now + next_plan["duration_days"] * 86400
        db_conn.execute(
            """UPDATE subscriptions
               SET plan_id=?, activated_at=?, expires_at=?, is_active=1, pending_plan_id=NULL
               WHERE vault_id=?""",
            (next_plan_id, now, expires, vault_id),
        )
        db_conn.commit()
    elif expired:
        db_conn.execute(
            "UPDATE subscriptions SET is_active=0 WHERE vault_id=?", (vault_id,)
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
    """Generate QR code encoding only the wallet address."""
    qrcode = _import_qrcode()
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10, border=4,
    )
    qr.add_data(address)
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

def _fmt_expiry(expires_at: int, tz_str: str) -> str:
    """Format expiry timestamp in user's timezone."""
    import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        tz  = ZoneInfo(tz_str or "UTC")
    except Exception:
        from zoneinfo import ZoneInfo
        tz  = ZoneInfo("UTC")
    dt = _dt.datetime.fromtimestamp(expires_at, tz=tz)
    return dt.strftime("%d %b %Y %H:%M %Z")

def _get_user_tz(vault_id: str) -> str:
    """Fetch user's timezone string from DB. Falls back to UTC."""
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute(
                "SELECT timezone FROM users WHERE vault_id=?", (vault_id,)
            ).fetchone()
        return (row["timezone"] or "UTC") if row else "UTC"
    except Exception:
        return "UTC"

# ═════════════════════════════════════════════════════════════════════════════
# ⑫ PAYMENT VERIFICATION  (Alchemy for EVM, public RPCs for others)
# ═════════════════════════════════════════════════════════════════════════════
def _alchemy_url(chain_id: str) -> str:
    template = ALCHEMY_URLS.get(chain_id, "")
    return template.format(key=get_alchemy_api_key())

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
    helius_key = None
    try:
        from bot import get_db
        with get_db() as db:
            row = db.execute("SELECT value FROM bot_settings WHERE key='helius_api_key'").fetchone()
        if row and row["value"]:
            helius_key = row["value"]
    except Exception:
        pass
    rpc = (f"https://mainnet.helius-rpc.com/?api-key={helius_key}"
           if helius_key else "https://api.mainnet-beta.solana.com")
    aiohttp = _import_aiohttp()
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
    return False, ""

# ═════════════════════════════════════════════════════════════════════════════
# ⑬ BACKGROUND INVOICE POLLER
# ═════════════════════════════════════════════════════════════════════════════
async def _invoice_poller(db_getter, bot):
    while True:
        await asyncio.sleep(INVOICE_POLL_INTERVAL)
        try:
            now = int(time.time())
            # Promote any expired subscriptions that have a queued (pending) plan
            with db_getter() as db:
                due_rows = db.execute(
                    """SELECT vault_id FROM subscriptions
                       WHERE pending_plan_id IS NOT NULL
                         AND expires_at IS NOT NULL AND expires_at <= ?""",
                    (now,),
                ).fetchall()
            for due in due_rows:
                vid = due["vault_id"]
                with db_getter() as db:
                    activate_pending_plan_if_expired(db, vid)
                # Notify the user their queued plan is now active
                try:
                    with db_getter() as db:
                        sub_row = db.execute(
                            "SELECT plan_id FROM subscriptions WHERE vault_id=?", (vid,)
                        ).fetchone()
                        user_row = db.execute(
                            "SELECT telegram_id FROM users WHERE vault_id=?", (vid,)
                        ).fetchone()
                    if sub_row and user_row:
                        plan = get_plan(sub_row["plan_id"])
                        await bot.send_message(
                            chat_id=user_row["telegram_id"],
                            text=(
                                f"✅ Your queued plan is now active!\n\n"
                                f"*{plan['name']}* has started.\n"
                                f"Thank you for subscribing! 🎉"
                            ),
                            parse_mode="Markdown",
                        )
                except Exception as e:
                    logger.error(f"Pending plan promotion notify error: {e}")
            with db_getter() as db:
                # Get pending invoices that just expired (for one-time final check)
                just_expired = db.execute(
                    """SELECT * FROM payment_invoices
                       WHERE status='pending' AND expires_at <= ?""",
                    (now,),
                ).fetchall()
            # Final payment check for just-expired invoices before marking expired
            for row in just_expired:
                invoice = dict(row)
                paid, tx_hash = await verify_payment(invoice)
                with db_getter() as db:
                    if paid:
                        db.execute(
                            """UPDATE payment_invoices
                               SET status='paid', paid_at=?, tx_hash=?
                               WHERE invoice_id=?""",
                            (now, tx_hash, invoice["invoice_id"]),
                        )
                        activate_subscription(db, invoice["vault_id"], invoice["plan_id"])
                    else:
                        db.execute(
                            "UPDATE payment_invoices SET status='expired' WHERE invoice_id=?",
                            (invoice["invoice_id"],),
                        )
                    db.commit()
                # Notify user
                plan = get_plan(invoice["plan_id"])
                try:
                    with db_getter() as db:
                        user_row = db.execute(
                            "SELECT telegram_id FROM users WHERE vault_id=?",
                            (invoice["vault_id"],)
                        ).fetchone()
                    if user_row:
                        if paid:
                            await bot.send_message(
                                chat_id=user_row["telegram_id"],
                                text=(
                                    f"✅ Payment confirmed!\n\n"
                                    f"Your *{plan['name']}* plan is now active.\n"
                                    f"Thank you for subscribing! 🎉"
                                ),
                                parse_mode="Markdown",
                            )
                        else:
                            await bot.send_message(
                                chat_id=user_row["telegram_id"],
                                text=(
                                    f"⌛ Your invoice for *{plan['name']}* has expired "
                                    f"and no payment was detected.\n\n"
                                    f"You can start a new payment anytime from Premium 💡."
                                ),
                                parse_mode="Markdown",
                            )
                except Exception as e:
                    logger.error(f"Poller notify (expired) error: {e}")

            # Normal check: still-pending, not-yet-expired invoices
            with db_getter() as db:
                still_pending = db.execute(
                    "SELECT * FROM payment_invoices WHERE status='pending' AND expires_at > ?",
                    (now,),
                ).fetchall()
            for row in still_pending:
                invoice = dict(row)
                paid, tx_hash = await verify_payment(invoice)
                if not paid:
                    continue
                with db_getter() as db:
                    db.execute(
                        """UPDATE payment_invoices
                           SET status='paid', paid_at=?, tx_hash=?
                           WHERE invoice_id=?""",
                        (now, tx_hash, invoice["invoice_id"]),
                    )
                    activate_subscription(db, invoice["vault_id"], invoice["plan_id"])
                    db.commit()
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
    from bot import _load_setting
    buttons = []
    buttons.append([InlineKeyboardButton("Basic", callback_data="sub_plan:basic")])
    if _load_setting("plus_premium_enabled", "1") != "0":
        buttons.append([InlineKeyboardButton("Plus", callback_data="sub_plan:plus")])
    if _load_setting("pro_premium_enabled", "1") != "0":
        buttons.append([InlineKeyboardButton("Pro",  callback_data="sub_plan:pro")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="main_menu")])
    return InlineKeyboardMarkup(buttons)

def _kb_plan_group(group: str) -> InlineKeyboardMarkup:
    """Keyboard shown after clicking Plus or Pro — shows only visible 30-day and yearly buy buttons."""
    from bot import _load_setting
    buttons = []
    if _load_setting(f"{group}_30_visible", "1") != "0":
        buttons.append([InlineKeyboardButton(f"Buy {group.capitalize()} 30 Day Pack", callback_data=f"sub_buy:{group}_30")])
    if _load_setting(f"{group}_year_visible", "1") != "0":
        buttons.append([InlineKeyboardButton(f"Buy {group.capitalize()} 1 Year Pack", callback_data=f"sub_buy:{group}_year")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="sub_plans")])
    return InlineKeyboardMarkup(buttons)

def _kb_tokens(plan_id: str) -> InlineKeyboardMarkup:
    group   = PLANS.get(plan_id, {}).get("plan_group", "plus")
    buttons = []
    row     = []
    for token in TOKENS:
        if is_token_enabled(group, token):
            emoji = "🔵" if token == "USDC" else "🟢"
            row.append(InlineKeyboardButton(f"{emoji} {token}", callback_data=f"sub_token:{plan_id}:{token}"))
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"sub_plan:{group}")])
    return InlineKeyboardMarkup(buttons)

def _kb_chains(plan_id: str, token: str) -> InlineKeyboardMarkup:
    group   = PLANS.get(plan_id, {}).get("plan_group", "plus")
    buttons = []
    row     = []
    for chain in SUPPORTED_CHAINS:
        if not is_network_enabled(group, chain["id"]):
            continue
        row.append(InlineKeyboardButton(
            f"{chain['logo']} {chain['name']}",
            callback_data=f"sub_chain:{plan_id}:{token}:{chain['id']}",
        ))
        if len(row) == 2:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    # Back goes to token selection for this plan_id
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data=f"sub_buy:{plan_id}")])
    return InlineKeyboardMarkup(buttons)

def _kb_invoice(invoice_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔍 Check Payment Status", callback_data=f"sub_check:{invoice_id}")],
        [InlineKeyboardButton("❌ Cancel Invoice",        callback_data=f"sub_cancel:{invoice_id}")],
    ])

def _kb_paid() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Done", callback_data="adm_noop")]])

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
    """Show plan group detail."""
    q     = update.callback_query; await q.answer()
    group = q.data.split(":", 1)[1]
    if group == "basic":
        # Show admin-set Basic message if available, else default
        basic_msg = None
        try:
            from bot import get_db
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM bot_settings WHERE key='basic_plan_message'"
                ).fetchone()
            if row and row["value"]:
                basic_msg = row["value"]
        except Exception:
            pass
        display_text = basic_msg if basic_msg else "This is free plan"
        await q.edit_message_text(
            display_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back", callback_data="sub_plans"),
            ]]),
        )
    elif group == "plus":
        default_text = (
            "Plus Plan\n\n"
            "$1.19 for 30 Days\n"
            "$14.28/year → $9.99/year with 30% off"
        )
        text = default_text
        try:
            from bot import get_db
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM bot_settings WHERE key='plus_plan_message'"
                ).fetchone()
            if row and row["value"]:
                text = row["value"]
        except Exception:
            pass
        await q.edit_message_text(text, reply_markup=_kb_plan_group(group))
    elif group == "pro":
        default_text = (
            "Pro Plan\n\n"
            "$2.49 for 30 Days\n"
            "$29.88/year → $14.90/year with 50% off"
        )
        text = default_text
        try:
            from bot import get_db
            with get_db() as db:
                row = db.execute(
                    "SELECT value FROM bot_settings WHERE key='pro_plan_message'"
                ).fetchone()
            if row and row["value"]:
                text = row["value"]
        except Exception:
            pass
        await q.edit_message_text(text, reply_markup=_kb_plan_group(group))
    else:
        await q.answer("Plan not found.", show_alert=True)

async def cb_sub_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q       = update.callback_query; await q.answer()
    plan_id = q.data.split(":", 1)[1]
    plan    = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    dur_label = "30 Days" if plan["duration_days"] == 30 else "1 Year"
    await q.edit_message_text(
        f"💳 *{plan['name']} — {dur_label}*\n\nAmount: *${plan['price_usd']:.2f}*\n\n"
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
                "You already have a pending invoice. Please cancel it or complete it, or try again after 2 hours when it expires automatically.",
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
    user_tz = _get_user_tz(vault)
    exp_str = _fmt_expiry(invoice["expires_at"], user_tz)
    # Split expiry into date and time parts
    exp_parts   = exp_str.split(" ")
    expiry_date = " ".join(exp_parts[:3]) if len(exp_parts) >= 3 else exp_str
    expiry_time = exp_parts[3] if len(exp_parts) >= 4 else ""
    duration_label = "30 Days" if invoice["plan_id"].endswith("_30") else "1 Year"
    explorer_url   = chain.get("explorer_url", "")

    # MarkdownV2 escape helper for dynamic values
    import re as _re
    def _esc(s: str) -> str:
        return _re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(s))

    qr_buf = await asyncio.to_thread(
        generate_payment_qr,
        invoice["address"], invoice["amount_usd"], token, chain,
    )
    caption = (
        f"💎 *Order Summary*\n"
        f"━━━━━━━━━━━━━━\n"
        f"📦 *Plan:* {_esc(plan['name'])}\n\n"
        f"⏱️ *Duration:* {_esc(duration_label)}\n\n"
        f"💵 *Amount:* {_esc(str(plan['price_usd']))} {_esc(token)}\n\n"
        f"🌐 *Network:* {_esc(chain['name'])}\n\n"
        f"📬 *Send To:*\n`{invoice['address']}`\n\n"
        f"⏳ *Expires:* {_esc(expiry_date)}, {_esc(expiry_time)} UTC\n\n"
        f"🆔 *Invoice ID:*\n`{invoice['invoice_id']}`\n\n"
        f"📝 *Instructions:*\n"
        f"1\\. Send EXACTLY {_esc(str(plan['price_usd']))} {_esc(token)} only\\.\n"
        f"2\\. Use {_esc(chain['name'])} network only\\. Sending on any other network will result in lost funds\\.\n"
        f"3\\. Double\\-check the address and QR code before confirming payment\\.\n\n"
        f"⚠️ *Important:*\n"
        f"\\- Pay first, then verify the transaction on {_esc(explorer_url)}\\.\n"
        f"\\- Once confirmed, click \"Check Payment Status\" in the bot and your plan will activate automatically\\.\n\n"
        f"🔒 *Safety Tip:* Save a screenshot of this invoice, address, and transaction\\. "
        f"If anything goes wrong, contact support with your Invoice ID, address, and screenshot for a fast resolution\\."
    )
    try:
        await q.message.delete()
    except Exception:
        pass
    await update.effective_chat.send_photo(
        photo=InputFile(qr_buf, filename="payment_qr.png"),
        caption=caption,
        parse_mode="MarkdownV2",
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
        user_tz   = _get_user_tz(invoice["vault_id"])
        exp_str   = _fmt_expiry(invoice["expires_at"], user_tz)
        await q.edit_message_caption(
            caption=(
                f"⏳ *Payment Pending*\n\n"
                f"Plan: *{plan['name']}*\n"
                f"Amount: `{invoice['amount_usd']:.2f}` {invoice['token']}\n"
                f"Network: {chain['logo']} {chain['name']}\n"
                f"Address: `{invoice['address']}`\n\n"
                f"⌛ Expires: {exp_str} ({remaining // 60} min left)\n\n"
                "Payment not detected yet. Wait for confirmation then check again."
            ),
            parse_mode="Markdown",
            reply_markup=_kb_invoice(invoice_id),
        )

async def cb_sub_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    from bot import get_db, get_session, kb_main
    q          = update.callback_query; await q.answer()
    invoice_id = q.data.split(":", 1)[1]
    with get_db() as db:
        db.execute(
            "UPDATE payment_invoices SET status='expired' WHERE invoice_id=? AND status='pending'",
            (invoice_id,),
        )
        db.commit()
    # Delete the QR/invoice message and send a fresh home message
    try:
        await q.message.delete()
    except Exception:
        pass
    uid   = update.effective_user.id
    vault = get_session(uid)
    if vault:
        await update.effective_chat.send_message(
            "❌ Invoice cancelled.\n\nChoose an option:",
            reply_markup=kb_main(),
        )
    else:
        await update.effective_chat.send_message(
            "❌ Invoice cancelled.",
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
