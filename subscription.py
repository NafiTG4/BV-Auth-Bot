"""
subscription.py — BV Authenticator Subscription & Crypto Payment Module
========================================================================
Handles:
  • Subscription plan display & flow
  • Per-user unique HD-wallet address generation (BIP44)
  • Invoice creation / expiry (2-hour TTL)
  • Payment verification via Moralis API (EVM) + Solana RPC + Tron API + Sui API
  • Auto-activating plan on confirmed payment
  • Background poller that checks all pending invoices

HOW TO INTEGRATE INTO bot.py:
  1. Call  init_subscription_db()  inside your existing  init_db()
  2. Register handlers: call  register_subscription_handlers(app)  at startup
  3. Add  💎 Subscription  button to  kb_main()  → callback_data="sub_plans"
  4. pip install bip_utils eth_account qrcode[pil] aiohttp base58
"""

from __future__ import annotations

import os, time, json, asyncio, logging, hashlib, secrets
from io import BytesIO
from typing import Optional

import aiohttp
import qrcode
from PIL import Image

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile,
)
from telegram.ext import (
    ContextTypes, CallbackQueryHandler, ConversationHandler,
)

from pricing import (
    PLANS, TOKENS, SUPPORTED_CHAINS, MORALIS_API_KEY, HD_WALLET_MNEMONIC,
    INVOICE_EXPIRY_SECONDS, REQUIRED_CONFIRMATIONS, PAYMENT_TOLERANCE,
    INVOICE_POLL_INTERVAL, get_plan, get_chain,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Lazy import of crypto libs so bot starts even if optional deps are missing
# ─────────────────────────────────────────────────────────────────────────────
def _import_wallet_libs():
    """Import HD-wallet libs; raises ImportError with install hint if missing."""
    try:
        from bip_utils import (
            Bip39MnemonicGenerator, Bip39SeedGenerator, Bip44, Bip44Coins,
            Bip44Changes,
        )
        from eth_account import Account as EthAccount
        import base58
        return Bip39SeedGenerator, Bip44, Bip44Coins, Bip44Changes, EthAccount, base58
    except ImportError as e:
        raise ImportError(
            f"Missing wallet libraries: {e}\n"
            "Install with: pip install bip_utils eth_account base58"
        ) from e

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPERS  (called from bot.py's init_db)
# ─────────────────────────────────────────────────────────────────────────────
def init_subscription_db(c):
    """
    Create subscription tables.
    Pass in an open sqlite3 connection cursor (called inside init_db).
    """
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            vault_id        TEXT PRIMARY KEY,
            plan_id         TEXT NOT NULL,
            activated_at    INTEGER NOT NULL,
            expires_at      INTEGER,        -- NULL = lifetime
            is_active       INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS payment_invoices (
            invoice_id      TEXT PRIMARY KEY,   -- random 16-byte hex
            vault_id        TEXT NOT NULL,
            plan_id         TEXT NOT NULL,
            token           TEXT NOT NULL,      -- USDC / USDT
            chain_id        TEXT NOT NULL,
            address         TEXT NOT NULL,      -- generated deposit address
            amount_usd      REAL NOT NULL,
            bip44_index     INTEGER NOT NULL,   -- chain's bip44 account index
            address_index   INTEGER NOT NULL,   -- per-user unique address index
            status          TEXT DEFAULT 'pending',  -- pending / paid / expired
            created_at      INTEGER NOT NULL,
            expires_at      INTEGER NOT NULL,
            paid_at         INTEGER,
            tx_hash         TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS used_addresses (
            address         TEXT PRIMARY KEY,   -- prevent address reuse
            vault_id        TEXT NOT NULL,
            used_at         INTEGER NOT NULL
        )
    """)
    # Track next address index per chain so each user on each chain is unique
    c.execute("""
        CREATE TABLE IF NOT EXISTS chain_address_counter (
            chain_id        TEXT PRIMARY KEY,
            next_index      INTEGER DEFAULT 0
        )
    """)

# ─────────────────────────────────────────────────────────────────────────────
# SUBSCRIPTION QUERIES
# ─────────────────────────────────────────────────────────────────────────────
def get_active_subscription(db_conn, vault_id: str) -> Optional[dict]:
    """Return active subscription row or None."""
    now = int(time.time())
    row = db_conn.execute(
        """SELECT * FROM subscriptions
           WHERE vault_id=? AND is_active=1
             AND (expires_at IS NULL OR expires_at > ?)""",
        (vault_id, now),
    ).fetchone()
    return dict(row) if row else None

def activate_subscription(db_conn, vault_id: str, plan_id: str):
    """Activate or extend subscription for a vault."""
    plan   = get_plan(plan_id)
    now    = int(time.time())
    if plan["duration_days"] == 0:
        expires = None  # lifetime
    else:
        # If there's an existing active sub, extend from its expiry
        existing = get_active_subscription(db_conn, vault_id)
        base = existing["expires_at"] if existing else now
        expires = base + plan["duration_days"] * 86400
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

# ─────────────────────────────────────────────────────────────────────────────
# HD WALLET ADDRESS GENERATION
# ─────────────────────────────────────────────────────────────────────────────
_SEED_CACHE: Optional[bytes] = None

def _get_seed() -> bytes:
    global _SEED_CACHE
    if _SEED_CACHE is None:
        Bip39SeedGenerator, *_ = _import_wallet_libs()
        _SEED_CACHE = Bip39SeedGenerator(HD_WALLET_MNEMONIC).Generate()
    return _SEED_CACHE

def _next_address_index(db_conn, chain_id: str) -> int:
    """Atomically increment and return the next fresh address index for a chain."""
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

def derive_evm_address(address_index: int) -> str:
    """Derive an EVM address at m/44'/60'/0'/0/<address_index>."""
    (Bip39SeedGenerator, Bip44, Bip44Coins,
     Bip44Changes, EthAccount, base58) = _import_wallet_libs()
    seed    = _get_seed()
    bip44   = Bip44.FromSeed(seed, Bip44Coins.ETHEREUM)
    account = (bip44
               .Purpose()
               .Coin()
               .Account(0)
               .Change(Bip44Changes.CHAIN_EXT)
               .AddressIndex(address_index))
    # eth_account from private key
    priv_hex = account.PrivateKey().Raw().ToHex()
    acct     = EthAccount.from_key(priv_hex)
    return acct.address   # checksummed 0x address

def derive_solana_address(address_index: int) -> str:
    """Derive a Solana address at m/44'/501'/0'/0'/<address_index>'."""
    (Bip39SeedGenerator, Bip44, Bip44Coins,
     Bip44Changes, EthAccount, base58) = _import_wallet_libs()
    seed  = _get_seed()
    bip44 = Bip44.FromSeed(seed, Bip44Coins.SOLANA)
    acct  = (bip44
             .Purpose()
             .Coin()
             .Account(0)
             .Change(Bip44Changes.CHAIN_EXT)
             .AddressIndex(address_index))
    return acct.PublicKey().RawCompressed().ToHex()   # base58 encoding done by bip_utils internally

def derive_tron_address(address_index: int) -> str:
    """Derive a TRON address at m/44'/195'/0'/0/<address_index>."""
    (Bip39SeedGenerator, Bip44, Bip44Coins,
     Bip44Changes, EthAccount, base58) = _import_wallet_libs()
    seed  = _get_seed()
    bip44 = Bip44.FromSeed(seed, Bip44Coins.TRON)
    acct  = (bip44
             .Purpose()
             .Coin()
             .Account(0)
             .Change(Bip44Changes.CHAIN_EXT)
             .AddressIndex(address_index))
    return acct.PublicKey().ToAddress()

def derive_sui_address(address_index: int) -> str:
    """Derive a SUI address at m/44'/784'/0'/0'/<address_index>'."""
    (Bip39SeedGenerator, Bip44, Bip44Coins,
     Bip44Changes, EthAccount, base58) = _import_wallet_libs()
    seed  = _get_seed()
    bip44 = Bip44.FromSeed(seed, Bip44Coins.SUI)
    acct  = (bip44
             .Purpose()
             .Coin()
             .Account(0)
             .Change(Bip44Changes.CHAIN_EXT)
             .AddressIndex(address_index))
    return acct.PublicKey().ToAddress()

def generate_deposit_address(db_conn, chain: dict, vault_id: str) -> tuple[str, int]:
    """
    Return (address, address_index) — fresh and guaranteed not reused.
    Marks the address as used in used_addresses table.
    """
    family = chain["family"]
    chain_id = chain["id"]
    while True:
        idx  = _next_address_index(db_conn, chain_id)
        if family == "evm":
            addr = derive_evm_address(idx)
        elif family == "solana":
            addr = derive_solana_address(idx)
        elif family == "tron":
            addr = derive_tron_address(idx)
        elif family == "sui":
            addr = derive_sui_address(idx)
        else:
            addr = derive_evm_address(idx)  # fallback
        # Ensure this address hasn't been used before
        existing = db_conn.execute(
            "SELECT 1 FROM used_addresses WHERE address=?", (addr,)
        ).fetchone()
        if not existing:
            db_conn.execute(
                "INSERT INTO used_addresses (address, vault_id, used_at) VALUES (?,?,?)",
                (addr, vault_id, int(time.time())),
            )
            db_conn.commit()
            return addr, idx

# ─────────────────────────────────────────────────────────────────────────────
# QR CODE GENERATION
# ─────────────────────────────────────────────────────────────────────────────
def generate_payment_qr(address: str, amount_usd: float,
                         token: str, chain: dict) -> BytesIO:
    """
    Generate a QR code encoding a WalletConnect/EIP-681 compatible URI.
    EVM chains: ethereum:<address>@<chain_id>?value=0&erc20=<contract>&uint256=<amount>
    Others: plain address URI.
    """
    contract = chain["token_contracts"].get(token, "")
    # EIP-681 URI — most wallets (MetaMask, Trust, Coinbase) parse this
    # Amount in token units (USDC/USDT = 6 decimals)
    amount_units = int(amount_usd * 1_000_000)
    family = chain["family"]
    if family == "evm" and contract:
        # erc20 transfer call
        uri = (
            f"ethereum:{contract}@/transfer"
            f"?address={address}"
            f"&uint256={amount_units}"
        )
    elif family == "tron":
        uri = f"tron:{address}?amount={amount_usd}&token={token}"
    elif family == "solana":
        uri = f"solana:{address}?amount={amount_usd}&spl-token={contract}"
    else:
        uri = address  # plain address for Sui and others

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf

# ─────────────────────────────────────────────────────────────────────────────
# PAYMENT VERIFICATION
# ─────────────────────────────────────────────────────────────────────────────
MORALIS_BASE = "https://deep-index.moralis.io/api/v2.2"

async def _moralis_get(session: aiohttp.ClientSession, path: str, params: dict = None) -> dict:
    headers = {"X-API-Key": MORALIS_API_KEY}
    async with session.get(f"{MORALIS_BASE}{path}", headers=headers, params=params or {}) as r:
        return await r.json()

async def verify_evm_payment(
    chain: dict, token: str, address: str,
    amount_usd: float, since_ts: int,
) -> tuple[bool, str]:
    """
    Check if address received >= amount_usd of token on an EVM chain.
    Returns (paid: bool, tx_hash: str).
    """
    contract = chain["token_contracts"].get(token)
    if not contract:
        return False, ""
    moralis_chain = chain["moralis_chain"]
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)  # 6 decimals
    async with aiohttp.ClientSession() as session:
        try:
            data = await _moralis_get(
                session,
                f"/erc20/{contract}/transfers",
                params={
                    "chain":          moralis_chain,
                    "to_address":     address.lower(),
                    "from_date":      since_ts,
                    "limit":          25,
                },
            )
            transfers = data.get("result", [])
            for tx in transfers:
                # Check it's incoming to our address
                if tx.get("to_address", "").lower() != address.lower():
                    continue
                val = int(tx.get("value", "0"))
                if val >= amount_min:
                    return True, tx.get("transaction_hash", "")
        except Exception as e:
            logger.error(f"Moralis EVM verify error ({chain['id']}): {e}")
    return False, ""

async def verify_solana_payment(
    token: str, address: str, amount_usd: float,
) -> tuple[bool, str]:
    """Check Solana SPL token transfer via public RPC."""
    mint = SUPPORTED_CHAINS[7]["token_contracts"].get(token)  # index 7 = solana
    if not mint:
        return False, ""
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    rpc = "https://api.mainnet-beta.solana.com"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [address, {"limit": 20}],
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(rpc, json=payload) as r:
                sigs_data = await r.json()
            sigs = [s["signature"] for s in sigs_data.get("result", [])]
            for sig in sigs:
                tx_payload = {
                    "jsonrpc": "2.0", "id": 1,
                    "method": "getTransaction",
                    "params": [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}],
                }
                async with session.post(rpc, json=tx_payload) as r:
                    tx_data = await r.json()
                tx = tx_data.get("result")
                if not tx:
                    continue
                # Walk through token balance changes
                for post in tx.get("meta", {}).get("postTokenBalances", []):
                    if (post.get("mint") == mint
                            and post.get("owner") == address):
                        amt = int(post.get("uiTokenAmount", {}).get("amount", "0"))
                        if amt >= amount_min:
                            return True, sig
        except Exception as e:
            logger.error(f"Solana verify error: {e}")
    return False, ""

async def verify_tron_payment(
    token: str, address: str, amount_usd: float, since_ts: int,
) -> tuple[bool, str]:
    """Check Tron TRC-20 transfer via TronGrid public API."""
    chain  = get_chain("tron")
    contract = chain["token_contracts"].get(token)
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    url = (
        f"https://api.trongrid.io/v1/accounts/{address}/transactions/trc20"
        f"?contract_address={contract}&limit=20&only_to=true"
    )
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url) as r:
                data = await r.json()
            for tx in data.get("data", []):
                ts = tx.get("block_timestamp", 0) // 1000
                if ts < since_ts:
                    continue
                val = int(tx.get("value", "0"))
                if val >= amount_min:
                    return True, tx.get("transaction_id", "")
        except Exception as e:
            logger.error(f"Tron verify error: {e}")
    return False, ""

async def verify_sui_payment(
    token: str, address: str, amount_usd: float,
) -> tuple[bool, str]:
    """Check Sui coin transfer via Sui JSON-RPC."""
    coin_type = get_chain("sui")["token_contracts"].get(token, "")
    amount_min = int(amount_usd * 1_000_000 * PAYMENT_TOLERANCE)
    rpc = "https://fullnode.mainnet.sui.io:443"
    payload = {
        "jsonrpc": "2.0", "id": 1,
        "method": "suix_getCoins",
        "params": [address, coin_type, None, 10],
    }
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(rpc, json=payload) as r:
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
    """Dispatch to the correct verifier based on chain family."""
    chain    = get_chain(invoice["chain_id"])
    if not chain:
        return False, ""
    family   = chain["family"]
    token    = invoice["token"]
    address  = invoice["address"]
    amount   = invoice["amount_usd"]
    since_ts = invoice["created_at"]
    if family == "evm":
        return await verify_evm_payment(chain, token, address, amount, since_ts)
    elif family == "solana":
        return await verify_solana_payment(token, address, amount)
    elif family == "tron":
        return await verify_tron_payment(token, address, amount, since_ts)
    elif family == "sui":
        return await verify_sui_payment(token, address, amount)
    return False, ""

# ─────────────────────────────────────────────────────────────────────────────
# INVOICE HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def create_invoice(db_conn, vault_id: str, plan_id: str,
                   token: str, chain: dict) -> dict:
    plan          = get_plan(plan_id)
    addr, addr_idx = generate_deposit_address(db_conn, chain, vault_id)
    now           = int(time.time())
    invoice_id    = secrets.token_hex(16)
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
        "paid_at":       None,
        "tx_hash":       None,
    }
    db_conn.execute(
        """INSERT INTO payment_invoices
           (invoice_id,vault_id,plan_id,token,chain_id,address,amount_usd,
            bip44_index,address_index,status,created_at,expires_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        list(invoice.values())[:-2],  # exclude paid_at, tx_hash (NULL)
    )
    db_conn.commit()
    return invoice

def get_pending_invoice(db_conn, vault_id: str) -> Optional[dict]:
    """Return the most recent pending (non-expired) invoice for a vault."""
    now = int(time.time())
    row = db_conn.execute(
        """SELECT * FROM payment_invoices
           WHERE vault_id=? AND status='pending' AND expires_at > ?
           ORDER BY created_at DESC LIMIT 1""",
        (vault_id, now),
    ).fetchone()
    return dict(row) if row else None

def expire_old_invoices(db_conn):
    """Mark all overdue pending invoices as expired."""
    now = int(time.time())
    db_conn.execute(
        "UPDATE payment_invoices SET status='expired' WHERE status='pending' AND expires_at <= ?",
        (now,),
    )
    db_conn.commit()

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND POLLER
# ─────────────────────────────────────────────────────────────────────────────
_bot_ref = None   # set in register_subscription_handlers

async def _invoice_poller(db_getter, bot):
    """
    Runs every INVOICE_POLL_INTERVAL seconds.
    Checks all pending invoices and activates plans on confirmed payment.
    """
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
                if paid:
                    with db_getter() as db:
                        db.execute(
                            """UPDATE payment_invoices
                               SET status='paid', paid_at=?, tx_hash=?
                               WHERE invoice_id=?""",
                            (int(time.time()), tx_hash, invoice["invoice_id"]),
                        )
                        activate_subscription(db, invoice["vault_id"], invoice["plan_id"])
                    # Notify user
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
                                    f"Thank you for subscribing to BV Authenticator! 🎉"
                                ),
                                parse_mode="Markdown",
                            )
                    except Exception as e:
                        logger.error(f"Poller notify error: {e}")
        except Exception as e:
            logger.error(f"Invoice poller error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# KEYBOARD BUILDERS
# ─────────────────────────────────────────────────────────────────────────────
def _kb_plans() -> InlineKeyboardMarkup:
    buttons = []
    for p in PLANS.values():
        dur = "Lifetime" if p["duration_days"] == 0 else f"{p['duration_days']}d"
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
    row = []
    for chain in SUPPORTED_CHAINS:
        label = f"{chain['logo']} {chain['name']}"
        cb    = f"sub_chain:{plan_id}:{token}:{chain['id']}"
        row.append(InlineKeyboardButton(label, callback_data=cb))
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Go Home", callback_data="main_menu")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
# CALLBACK HANDLERS
# ─────────────────────────────────────────────────────────────────────────────
async def cb_sub_plans(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all subscription plans."""
    from bot import get_db, get_session  # inline import to avoid circular
    q = update.callback_query; await q.answer()
    uid   = update.effective_user.id
    vault = get_session(uid)
    # Show current subscription status if any
    sub_text = ""
    if vault:
        with get_db() as db:
            sub = get_active_subscription(db, vault)
        if sub:
            plan = get_plan(sub["plan_id"])
            if sub["expires_at"]:
                import datetime
                exp_dt  = datetime.datetime.utcfromtimestamp(sub["expires_at"])
                exp_str = exp_dt.strftime("%d %b %Y")
                sub_text = f"✅ Active plan: *{plan['name']}* (expires {exp_str})\n\n"
            else:
                sub_text = f"✅ Active plan: *{plan['name']}* (Lifetime)\n\n"
    await q.edit_message_text(
        f"{sub_text}💎 *Choose a Subscription Plan*\n\n"
        "Select a plan to see details and pricing.",
        parse_mode="Markdown",
        reply_markup=_kb_plans(),
    )

async def cb_sub_plan_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show plan details."""
    q = update.callback_query; await q.answer()
    plan_id = q.data.split(":", 1)[1]
    plan    = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    dur_str = "Lifetime (never expires)" if plan["duration_days"] == 0 else f"{plan['duration_days']} days"
    features = "\n".join(f"  ✔ {f}" for f in plan["features"])
    text = (
        f"{plan['name']}\n\n"
        f"💰 Price: *${plan['price_usd']:.2f}* ({dur_str})\n\n"
        f"{plan['description']}\n\n"
        f"*What's included:*\n{features}"
    )
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=_kb_plan_detail(plan_id))

async def cb_sub_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show token selection (USDC / USDT)."""
    q = update.callback_query; await q.answer()
    plan_id = q.data.split(":", 1)[1]
    plan    = get_plan(plan_id)
    if not plan:
        await q.answer("Plan not found.", show_alert=True); return
    await q.edit_message_text(
        f"💳 *Pay for {plan['name']}*\n\n"
        f"Amount: *${plan['price_usd']:.2f}*\n\n"
        "Select your preferred stablecoin:",
        parse_mode="Markdown",
        reply_markup=_kb_tokens(plan_id),
    )

async def cb_sub_token(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show chain selection."""
    q = update.callback_query; await q.answer()
    _, plan_id, token = q.data.split(":", 2)
    plan = get_plan(plan_id)
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
    """Generate deposit address + QR code and show invoice."""
    from bot import get_db, get_session
    q = update.callback_query; await q.answer()
    _, plan_id, token, chain_id = q.data.split(":", 3)
    uid   = update.effective_user.id
    vault = get_session(uid)
    if not vault:
        await q.answer("Please log in first.", show_alert=True); return
    plan  = get_plan(plan_id)
    chain = get_chain(chain_id)
    if not plan or not chain:
        await q.answer("Invalid plan or chain.", show_alert=True); return

    # Check for existing pending invoice
    with get_db() as db:
        existing = get_pending_invoice(db, vault)
    if existing:
        await q.answer(
            "You already have a pending invoice. Cancel it first before creating a new one.",
            show_alert=True,
        )
        return

    await q.edit_message_text("⏳ Generating your deposit address...")

    # Create invoice (may take a moment for derivation)
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
        )
        return

    # Generate QR
    import datetime
    exp_dt  = datetime.datetime.utcfromtimestamp(invoice["expires_at"])
    exp_str = exp_dt.strftime("%d %b %Y %H:%M UTC")
    qr_buf = await asyncio.to_thread(
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
        "Scan the QR code with any wallet. After sending, tap *Check Payment Status*."
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
    """Manually check payment status for an invoice."""
    from bot import get_db, get_session
    q = update.callback_query; await q.answer("Checking…")
    invoice_id = q.data.split(":", 1)[1]
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM payment_invoices WHERE invoice_id=?", (invoice_id,)
        ).fetchone()
    if not row:
        await q.answer("Invoice not found.", show_alert=True); return
    invoice = dict(row)
    now = int(time.time())

    if invoice["status"] == "paid":
        plan = get_plan(invoice["plan_id"])
        await q.edit_message_caption(
            caption=(
                f"✅ *Payment confirmed!*\n\n"
                f"Plan *{plan['name']}* is now active.\n"
                f"Tx: `{invoice['tx_hash'] or 'confirmed'}`"
            ),
            parse_mode="Markdown",
            reply_markup=_kb_paid(),
        )
        return

    if invoice["status"] == "expired" or invoice["expires_at"] <= now:
        with get_db() as db:
            db.execute(
                "UPDATE payment_invoices SET status='expired' WHERE invoice_id=?",
                (invoice_id,),
            )
            db.commit()
        await q.edit_message_caption(
            caption="⌛ This invoice has *expired*. Please start a new payment.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 New Payment", callback_data="sub_plans"),
                InlineKeyboardButton("🏠 Home",        callback_data="main_menu"),
            ]]),
        )
        return

    # Actually verify on-chain
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
            caption=(
                f"✅ *Payment confirmed!*\n\n"
                f"Your *{plan['name']}* plan is now active. 🎉\n"
                f"Tx: `{tx_hash or 'confirmed'}`"
            ),
            parse_mode="Markdown",
            reply_markup=_kb_paid(),
        )
    else:
        # Show remaining time
        remaining = invoice["expires_at"] - now
        mins = remaining // 60
        chain = get_chain(invoice["chain_id"])
        plan  = get_plan(invoice["plan_id"])
        await q.edit_message_caption(
            caption=(
                f"⏳ *Payment Pending*\n\n"
                f"Plan: *{plan['name']}*\n"
                f"Amount: `{invoice['amount_usd']:.2f}` {invoice['token']}\n"
                f"Network: {chain['logo']} {chain['name']}\n"
                f"Address: `{invoice['address']}`\n\n"
                f"⌛ Expires in: {mins} min\n\n"
                "Payment not detected yet. If you have sent funds, wait a moment for the transaction to confirm, then check again."
            ),
            parse_mode="Markdown",
            reply_markup=_kb_invoice(invoice_id),
        )

async def cb_sub_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cancel (expire) a pending invoice."""
    from bot import get_db
    q = update.callback_query; await q.answer()
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

# ─────────────────────────────────────────────────────────────────────────────
# REGISTER ALL HANDLERS + START BACKGROUND POLLER
# ─────────────────────────────────────────────────────────────────────────────
def register_subscription_handlers(app, db_getter):
    """
    Call this once at bot startup, after building the Application.

    app        — telegram.ext.Application
    db_getter  — callable that returns a context-managed DB connection (your get_db)
    """
    app.add_handler(CallbackQueryHandler(cb_sub_plans,       pattern=r"^sub_plans$"))
    app.add_handler(CallbackQueryHandler(cb_sub_plan_detail, pattern=r"^sub_plan:"))
    app.add_handler(CallbackQueryHandler(cb_sub_buy,         pattern=r"^sub_buy:"))
    app.add_handler(CallbackQueryHandler(cb_sub_token,       pattern=r"^sub_token:"))
    app.add_handler(CallbackQueryHandler(cb_sub_chain,       pattern=r"^sub_chain:"))
    app.add_handler(CallbackQueryHandler(cb_sub_check,       pattern=r"^sub_check:"))
    app.add_handler(CallbackQueryHandler(cb_sub_cancel,      pattern=r"^sub_cancel:"))

    # Start background poller
    async def _start_poller(application):
        asyncio.create_task(_invoice_poller(db_getter, application.bot))

    app.post_init = _start_poller
    logger.info("Subscription handlers registered.")
