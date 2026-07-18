#!/usr/bin/env python3
"""
Hyperliquid Wallet Monitor
Sends Telegram alerts when tracked wallets make trades.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Optional

import websockets
from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Hyperliquid WebSocket endpoint
WS_URL = "wss://api.hyperliquid.xyz/ws"


class WalletManager:
    """Manages reading and writing wallets.json"""

    def __init__(self):
        self.wallets_path = Path(__file__).parent / "wallets.json"
        self.data = self._load()

    def _load(self) -> dict:
        if self.wallets_path.exists():
            with open(self.wallets_path, 'r') as f:
                return json.load(f)
        return {"wallets": []}

    def _save(self):
        with open(self.wallets_path, 'w') as f:
            json.dump(self.data, f, indent=4, ensure_ascii=False)

    @property
    def wallets(self) -> list:
        return self.data.get('wallets', [])

    def add_wallet(self, address: str, label: str) -> bool:
        """Add a wallet. Returns False if already exists."""
        addr_lower = address.lower()
        for w in self.wallets:
            if w['address'].lower() == addr_lower:
                return False
        self.data.setdefault('wallets', []).append({
            'address': address,
            'label': label
        })
        self._save()
        return True

    def remove_wallet(self, identifier: str) -> Optional[dict]:
        """Remove wallet by address or label. Returns removed wallet or None."""
        id_lower = identifier.lower()
        for i, w in enumerate(self.wallets):
            if w['address'].lower() == id_lower or w['label'].lower() == id_lower:
                removed = self.wallets.pop(i)
                self._save()
                return removed
        return None

    def find_wallet(self, identifier: str) -> Optional[dict]:
        """Find wallet by address or label."""
        id_lower = identifier.lower()
        for w in self.wallets:
            if w['address'].lower() == id_lower or w['label'].lower() == id_lower:
                return w
        return None


def load_config() -> dict:
    """Load configuration from config.json and wallets from wallets.json"""
    config_path = Path(__file__).parent / "config.json"
    wallets_path = Path(__file__).parent / "wallets.json"

    if not config_path.exists():
        logger.error("config.json not found! Copy config.example.json to config.json and fill in your details.")
        raise FileNotFoundError("config.json not found")

    with open(config_path, 'r') as f:
        config = json.load(f)

    # Load wallets and settings from separate file (can be committed to git)
    if wallets_path.exists():
        with open(wallets_path, 'r') as f:
            wallets_data = json.load(f)
            config['wallets'] = wallets_data.get('wallets', [])
        logger.info(f"Loaded {len(config['wallets'])} wallet(s) from wallets.json")
    elif 'wallets' not in config:
        config['wallets'] = []

    return config


def format_address(address: str) -> str:
    """Shorten wallet address for display"""
    return f"{address[:6]}...{address[-4:]}"


def format_size(size: float) -> str:
    """Format size with appropriate units"""
    if size >= 1_000_000:
        return f"{size/1_000_000:.2f}M"
    elif size >= 1_000:
        return f"{size/1_000:.2f}K"
    else:
        return f"{size:.4f}"


def format_alert(fill: dict, label: str, address: str) -> str:
    """Format a fill into a Telegram alert message"""
    coin = fill.get('coin', 'UNKNOWN')
    side = fill.get('side', '')
    size = float(fill.get('sz', 0))
    price = float(fill.get('px', 0))
    closed_pnl = float(fill.get('closedPnl', 0))

    # Determine direction based on side and whether it's opening or closing
    # closedPnl != 0 means closing a position
    is_close = closed_pnl != 0

    if side == 'B':  # Buy/Bid
        if is_close:
            direction = "CLOSE SHORT"
            emoji = "🟢"  # Green = closing short (profitable exit from short)
        else:
            direction = "OPEN LONG"
            emoji = "🟢"
    elif side == 'A':  # Sell/Ask
        if is_close:
            direction = "CLOSE LONG"
            emoji = "🔴"  # Red = closing long
        else:
            direction = "OPEN SHORT"
            emoji = "🔴"
    else:
        direction = side
        emoji = "⚪"

    # Calculate USD value
    usd_value = size * price

    message = f"""
{emoji} <b>{label}</b> - {direction}

<b>Coin:</b> {coin}
<b>Size:</b> ${usd_value:,.0f}

https://hypurrscan.io/address/{address}
"""
    return message.strip()


class HyperliquidMonitor:
    def __init__(self, config: dict, wallet_manager: WalletManager):
        self.config = config
        self.wallet_manager = wallet_manager
        self.bot = Bot(token=config['telegram']['bot_token'])
        self.chat_id = config['telegram']['chat_id']
        self._ws = None  # Active WebSocket reference

        # Create address -> label mapping
        self.wallets = {
            w['address'].lower(): w['label']
            for w in wallet_manager.wallets
        }

        # Create address -> allowed coins mapping (None = all coins allowed)
        self.wallet_coins = {
            w['address'].lower(): [c.upper() for c in w['coins']] if 'coins' in w else None
            for w in wallet_manager.wallets
        }

        # Track processed fills to avoid duplicates
        self.processed_fills = set()

        # Store start time to ignore historical fills (in milliseconds)
        self.start_time = int(time.time() * 1000)

        # TWAP detection: track fill counts per wallet+coin+type
        # Key: "{address}_{coin}_{open/close}", Value: {"count": int, "last_time": int}
        self.fill_series = {}
        self.TWAP_ALERT_LIMIT = 1  # Max alerts before assuming TWAP
        self.TWAP_RESET_MS = 5 * 60 * 1000  # 5 minutes in milliseconds

    async def send_alert(self, message: str):
        """Send alert to Telegram"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
            logger.info("Alert sent successfully")
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")

    async def add_wallet(self, address: str, label: str) -> bool:
        """Add wallet to monitoring and subscribe on active WebSocket"""
        if not self.wallet_manager.add_wallet(address, label):
            return False
        addr_lower = address.lower()
        self.wallets[addr_lower] = label
        self.wallet_coins[addr_lower] = None
        if self._ws:
            await self.subscribe_to_wallet(self._ws, address)
        return True

    async def remove_wallet(self, identifier: str) -> Optional[dict]:
        """Remove wallet from monitoring"""
        removed = self.wallet_manager.remove_wallet(identifier)
        if removed:
            addr_lower = removed['address'].lower()
            self.wallets.pop(addr_lower, None)
            self.wallet_coins.pop(addr_lower, None)
        return removed

    async def handle_fill(self, fill: dict, address: str):
        """Process a fill event"""
        # Ignore fills from wallets no longer tracked
        if address.lower() not in self.wallets:
            return

        # Check coin filter for this wallet
        coin = fill.get('coin', 'UNKNOWN')
        allowed_coins = self.wallet_coins.get(address.lower())
        if allowed_coins is not None and coin.upper() not in allowed_coins:
            return  # Skip coins not in the filter

        # Ignore fills that happened before monitor started
        fill_time = fill.get('time', 0)
        if fill_time < self.start_time:
            return

        # Create unique ID for deduplication
        fill_id = f"{address}_{fill.get('tid', '')}_{fill_time}"

        if fill_id in self.processed_fills:
            return

        self.processed_fills.add(fill_id)

        # Keep set from growing too large
        if len(self.processed_fills) > 10000:
            self.processed_fills = set(list(self.processed_fills)[-5000:])

        # Determine fill type for TWAP tracking
        coin = fill.get('coin', 'UNKNOWN')
        closed_pnl = float(fill.get('closedPnl', 0))
        fill_type = "close" if closed_pnl != 0 else "open"

        # TWAP detection key
        series_key = f"{address.lower()}_{coin}_{fill_type}"

        # Check if series should reset (5 min gap)
        if series_key in self.fill_series:
            last_time = self.fill_series[series_key]["last_time"]
            if fill_time - last_time > self.TWAP_RESET_MS:
                # Reset series after 5 min gap
                self.fill_series[series_key] = {"count": 0, "last_time": fill_time}
        else:
            self.fill_series[series_key] = {"count": 0, "last_time": fill_time}

        # Increment count and update time
        self.fill_series[series_key]["count"] += 1
        self.fill_series[series_key]["last_time"] = fill_time

        # Check if we should send alert (only first in series)
        count = self.fill_series[series_key]["count"]
        if count > self.TWAP_ALERT_LIMIT:
            logger.info(f"TWAP assumed, skipping alert: {coin} {fill_type} (fill #{count})")
            return

        label = self.wallets.get(address.lower(), format_address(address))
        message = format_alert(fill, label, address)

        logger.info(f"New fill detected: {label} - {coin} {fill_type} (fill #{count})")
        await self.send_alert(message)

    async def subscribe_to_wallet(self, ws, address: str):
        """Subscribe to fills for a wallet"""
        subscribe_msg = {
            "method": "subscribe",
            "subscription": {
                "type": "userFills",
                "user": address
            }
        }
        await ws.send(json.dumps(subscribe_msg))
        logger.info(f"Subscribed to fills for {self.wallets.get(address.lower(), address)}")

    async def monitor(self):
        """Main monitoring loop"""
        while True:
            try:
                logger.info(f"Connecting to Hyperliquid WebSocket...")

                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    logger.info("Connected! Subscribing to wallets...")

                    # Subscribe to all wallets
                    for address in self.wallets.keys():
                        await self.subscribe_to_wallet(ws, address)

                    logger.info(f"Monitoring {len(self.wallets)} wallet(s). Waiting for trades...")

                    # Listen for messages
                    async for message in ws:
                        try:
                            data = json.loads(message)

                            # Handle fill events
                            if data.get('channel') == 'userFills':
                                fills = data.get('data', [])
                                user = data.get('data', {}).get('user', '') if isinstance(data.get('data'), dict) else ''

                                # Handle different response formats
                                if isinstance(fills, dict):
                                    user = fills.get('user', '')
                                    fills = fills.get('fills', [])

                                for fill in fills:
                                    if isinstance(fill, dict):
                                        # Try to get user from fill or from parent
                                        fill_user = fill.get('user', user)
                                        if fill_user:
                                            await self.handle_fill(fill, fill_user)
                                        else:
                                            # Find which wallet this belongs to
                                            for addr in self.wallets.keys():
                                                await self.handle_fill(fill, addr)
                                                break

                        except json.JSONDecodeError:
                            logger.warning(f"Invalid JSON received: {message[:100]}")
                        except Exception as e:
                            logger.error(f"Error processing message: {e}")

            except websockets.exceptions.ConnectionClosed:
                self._ws = None
                logger.warning("WebSocket connection closed. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                self._ws = None
                logger.error(f"Connection error: {e}. Reconnecting in 10 seconds...")
                await asyncio.sleep(10)


def _check_auth(chat_id: str):
    """Decorator factory: only allow commands from the configured chat_id."""
    def decorator(func):
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if str(update.effective_chat.id) != str(chat_id):
                return  # silently ignore unauthorized users
            return await func(update, context)
        return wrapper
    return decorator


def make_handlers(wallet_monitor: HyperliquidMonitor, wallet_manager: WalletManager, chat_id: str):
    """Create command handler functions bound to the monitor instances."""
    auth = _check_auth(chat_id)

    @auth
    async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
        wallets = wallet_manager.wallets
        if not wallets:
            await update.message.reply_text("Ei seurattavia lompakoita.")
            return
        lines = []
        for i, w in enumerate(wallets, 1):
            coins_str = f" [{', '.join(w['coins'])}]" if 'coins' in w else ""
            lines.append(f"{i}. <b>{w['label']}</b>{coins_str}\n<code>{w['address']}</code>")
        text = "\n\n".join(lines)
        await update.message.reply_text(f"Seuratut lompakot ({len(wallets)}):\n\n{text}", parse_mode=ParseMode.HTML)

    @auth
    async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args or len(args) < 2:
            await update.message.reply_text("Käyttö: /add <osoite> <nimi>")
            return
        address = args[0]
        label = " ".join(args[1:])

        if not address.startswith("0x") or len(address) != 42:
            await update.message.reply_text("Virheellinen osoite. Osoitteen tulee alkaa 0x ja olla 42 merkkiä.")
            return

        if await wallet_monitor.add_wallet(address, label):
            await update.message.reply_text(
                f"Lompakko lisätty seurantaan:\n<b>{label}</b>\n<code>{address}</code>",
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Wallet added via Telegram: {label} ({address})")
        else:
            await update.message.reply_text("Tämä lompakko on jo seurannassa.")

    @auth
    async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Käyttö: /remove <nimi tai osoite>")
            return
        identifier = " ".join(args)
        removed = await wallet_monitor.remove_wallet(identifier)
        if removed:
            await update.message.reply_text(
                f"Lompakko poistettu:\n<b>{removed['label']}</b>\n<code>{removed['address']}</code>",
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Wallet removed via Telegram: {removed['label']} ({removed['address']})")
        else:
            await update.message.reply_text("Lompakkoa ei löytynyt nimellä tai osoitteella.")

    return cmd_list, cmd_add, cmd_remove


async def main():
    """Entry point"""
    logger.info("=" * 50)
    logger.info("Hyperliquid Monitor")
    logger.info("=" * 50)

    try:
        config = load_config()
        wallet_manager = WalletManager()

        # Build PTB Application for command handling
        app = Application.builder().token(config['telegram']['bot_token']).build()

        # Create wallet monitor (always, commands need it)
        wallet_monitor = HyperliquidMonitor(config, wallet_manager)

        # Register command handlers
        chat_id = config['telegram']['chat_id']
        cmd_list, cmd_add, cmd_remove = make_handlers(wallet_monitor, wallet_manager, chat_id)
        app.add_handler(CommandHandler("list", cmd_list))
        app.add_handler(CommandHandler("add", cmd_add))
        app.add_handler(CommandHandler("remove", cmd_remove))

        # Initialize the application (sets up bot, updater, etc.)
        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram command handlers registered: /list, /add, /remove")

        # Start wallet monitor if wallets configured
        if wallet_manager.wallets:
            logger.info(f"Loaded {len(wallet_manager.wallets)} wallet(s) from wallets.json")
        else:
            logger.warning("No wallets configured yet - use /add to add wallets")

        await wallet_monitor.monitor()

    except FileNotFoundError:
        logger.error("Please create config.json from config.example.json")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
    finally:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
