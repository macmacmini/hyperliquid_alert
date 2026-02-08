#!/usr/bin/env python3
"""
Hyperliquid Wallet Monitor
Sends Telegram alerts when tracked wallets make trades.
Also monitors top HYPE spot holders for new whales.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp
import websockets
from telegram import Bot
from telegram.constants import ParseMode

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Hyperliquid WebSocket endpoint
WS_URL = "wss://api.hyperliquid.xyz/ws"

# HypurrScan API for whale monitoring
HYPURRSCAN_API = "https://api.hypurrscan.io"

# Protocol addresses to filter out (staking, burn, etc.)
PROTOCOL_ADDRESSES = {
    "0x2222222222222222222222222222222222222222",
    "0xfefefefefefefefefefefefefefefefefefefefe",
    "0x0000000000000000000000000000000000000000",
}


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
            # Also load whale_monitor settings if present
            if 'whale_monitor' in wallets_data:
                config['whale_monitor'] = wallets_data['whale_monitor']
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


def format_balance(balance: float) -> str:
    """Format HYPE balance with appropriate units"""
    if balance >= 1_000_000:
        return f"{balance/1_000_000:.2f}M"
    elif balance >= 1_000:
        return f"{balance/1_000:.1f}K"
    else:
        return f"{balance:.0f}"


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
    def __init__(self, config: dict):
        self.config = config
        self.bot = Bot(token=config['telegram']['bot_token'])
        self.chat_id = config['telegram']['chat_id']

        # Create address -> label mapping
        self.wallets = {
            w['address'].lower(): w['label']
            for w in config['wallets']
        }

        # Create address -> allowed coins mapping (None = all coins allowed)
        self.wallet_coins = {
            w['address'].lower(): [c.upper() for c in w['coins']] if 'coins' in w else None
            for w in config['wallets']
        }

        # Track processed fills to avoid duplicates
        self.processed_fills = set()

        # Store start time to ignore historical fills (in milliseconds)
        self.start_time = int(time.time() * 1000)

        # TWAP detection: track fill counts per wallet+coin+type
        # Key: "{address}_{coin}_{open/close}", Value: {"count": int, "last_time": int}
        self.fill_series = {}
        self.TWAP_ALERT_LIMIT = 1  # Max alerts before assuming TWAP
        self.TWAP_RESET_MS = 1 * 60 * 1000  # 1 minute in milliseconds

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

    async def handle_fill(self, fill: dict, address: str):
        """Process a fill event"""
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
                logger.warning("WebSocket connection closed. Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Connection error: {e}. Reconnecting in 10 seconds...")
                await asyncio.sleep(10)


class WhaleMonitor:
    """Monitors top HYPE spot holders and alerts on rapid balance changes"""

    def __init__(self, config: dict):
        self.config = config
        self.bot = Bot(token=config['telegram']['bot_token'])
        self.chat_id = config['telegram']['chat_id']

        # Whale settings from config
        whale_config = config.get('whale_monitor', {})
        self.min_balance = whale_config.get('min_hype_balance', 100_000)
        self.poll_interval = whale_config.get('poll_interval_seconds', 300)
        self.top_holders_limit = whale_config.get('top_holders_limit', 100)

        # Hourly change detection settings
        self.time_window_seconds = whale_config.get('time_window_minutes', 60) * 60  # 1 hour default
        self.min_hourly_change = whale_config.get('min_hourly_change_hype', 50_000)  # 50K HYPE default
        self.alert_cooldown_seconds = whale_config.get('alert_cooldown_minutes', 30) * 60  # 30 min cooldown

        # Known exchange addresses (loaded from API)
        self.exchange_addresses = set()

        # Balance history: {address: [(timestamp, balance), ...]}
        self.balance_history = {}

        # Alert cooldown tracking: {address: last_alert_timestamp}
        self.last_alert_time = {}

        # Track known whales (for new whale detection)
        self.known_whales_file = Path(__file__).parent / "known_whales.json"
        self.known_whales = self._load_known_whales()

    def _load_known_whales(self) -> dict:
        """Load known whales from file"""
        if self.known_whales_file.exists():
            try:
                with open(self.known_whales_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}

    def _save_known_whales(self):
        """Save known whales to file"""
        with open(self.known_whales_file, 'w') as f:
            json.dump(self.known_whales, f, indent=2)

    async def fetch_exchange_addresses(self, session: aiohttp.ClientSession):
        """Fetch known exchange addresses from HypurrScan"""
        try:
            async with session.get(f"{HYPURRSCAN_API}/globalAliases") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    exchanges = set()

                    if isinstance(data, dict):
                        for category, addresses in data.items():
                            cat_lower = category.lower()
                            if any(x in cat_lower for x in ['exchange', 'cex', 'gate', 'kucoin', 'okx', 'bybit', 'binance', 'bitget', 'mexc', 'robinhood', 'coinbase', 'bitvavo']):
                                if isinstance(addresses, list):
                                    for addr in addresses:
                                        if isinstance(addr, str):
                                            exchanges.add(addr.lower())
                                elif isinstance(addresses, str):
                                    exchanges.add(addresses.lower())

                    self.exchange_addresses = exchanges
                    logger.info(f"[Whale] Loaded {len(exchanges)} exchange addresses")
        except Exception as e:
            logger.error(f"[Whale] Failed to fetch exchange addresses: {e}")

    async def fetch_top_holders(self, session: aiohttp.ClientSession) -> list:
        """Fetch top HYPE holders from HypurrScan"""
        try:
            url = f"{HYPURRSCAN_API}/holdersWithLimit/HYPE/{self.top_holders_limit}"
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if isinstance(data, list):
                        return data
                    elif isinstance(data, dict):
                        return data.get('holders', data.get('data', []))
        except Exception as e:
            logger.error(f"[Whale] Failed to fetch top holders: {e}")
        return []

    def is_filtered_address(self, address: str) -> bool:
        """Check if address should be filtered out"""
        addr_lower = address.lower()

        if addr_lower in PROTOCOL_ADDRESSES:
            return True
        if addr_lower in self.exchange_addresses:
            return True
        # Filter pattern addresses like 0x2222...2222
        if len(addr_lower) >= 10 and addr_lower[2:6] == addr_lower[6:10] == addr_lower[-4:]:
            return True

        return False

    async def send_alert(self, message: str):
        """Send alert to Telegram"""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=message,
                parse_mode=ParseMode.HTML
            )
            logger.info("[Whale] Alert sent successfully")
        except Exception as e:
            logger.error(f"[Whale] Failed to send alert: {e}")

    def _update_balance_history(self, address: str, balance: float, current_time: int):
        """Update balance history and clean old entries"""
        if address not in self.balance_history:
            self.balance_history[address] = []

        # Add current balance
        self.balance_history[address].append((current_time, balance))

        # Remove entries older than time window
        cutoff_time = current_time - self.time_window_seconds
        self.balance_history[address] = [
            (ts, bal) for ts, bal in self.balance_history[address]
            if ts >= cutoff_time
        ]

    def _get_hourly_change(self, address: str, current_balance: float, current_time: int) -> tuple:
        """Calculate balance change over the time window. Returns (change, oldest_balance, time_span_minutes)"""
        if address not in self.balance_history or len(self.balance_history[address]) < 2:
            return 0, current_balance, 0

        # Get oldest entry within the window
        oldest_ts, oldest_balance = self.balance_history[address][0]
        time_span_minutes = (current_time - oldest_ts) / 60

        change = current_balance - oldest_balance
        return change, oldest_balance, time_span_minutes

    def _can_send_alert(self, address: str, current_time: int) -> bool:
        """Check if we can send alert (cooldown check)"""
        last_alert = self.last_alert_time.get(address, 0)
        return (current_time - last_alert) >= self.alert_cooldown_seconds

    async def check_for_new_whales(self, holders: list):
        """Check for new whales and rapid balance changes"""
        current_time = int(time.time())

        for holder in holders:
            # Extract address and balance
            if isinstance(holder, dict):
                address = holder.get('address', holder.get('user', '')).lower()
                balance = float(holder.get('balance', holder.get('amount', 0)))
            elif isinstance(holder, list) and len(holder) >= 2:
                address = str(holder[0]).lower()
                balance = float(holder[1])
            else:
                continue

            if not address or self.is_filtered_address(address):
                continue

            if balance < self.min_balance:
                continue

            # Update balance history
            self._update_balance_history(address, balance, current_time)

            # New whale detection
            if address not in self.known_whales:
                self.known_whales[address] = {
                    'first_seen': current_time,
                    'initial_balance': balance
                }

                usd_value = balance * 30  # Approximate HYPE price

                message = f"""
🐋 <b>NEW HYPE WHALE</b>

<b>Address:</b> <code>{address}</code>
<b>Balance:</b> {format_balance(balance)} HYPE
<b>Est. Value:</b> ~${usd_value:,.0f}

https://hypurrscan.io/address/{address}
"""
                await self.send_alert(message.strip())
                self.last_alert_time[address] = current_time
                logger.info(f"[Whale] New whale: {format_address(address)} with {format_balance(balance)} HYPE")

            else:
                # Check for rapid balance change (within time window)
                change, old_balance, time_span = self._get_hourly_change(address, balance, current_time)

                # Only alert if: change > threshold AND enough time has passed AND cooldown expired
                if abs(change) >= self.min_hourly_change and time_span >= 5 and self._can_send_alert(address, current_time):
                    direction = "BOUGHT" if change > 0 else "SOLD"
                    emoji = "🟢" if change > 0 else "🔴"
                    change_pct = (change / old_balance * 100) if old_balance > 0 else 0

                    message = f"""
{emoji} <b>RAPID HYPE MOVEMENT</b>

<b>Address:</b> <code>{address}</code>
<b>Action:</b> {direction} {format_balance(abs(change))} HYPE
<b>Time:</b> ~{time_span:.0f} min
<b>New Balance:</b> {format_balance(balance)} HYPE ({change_pct:+.1f}%)

https://hypurrscan.io/address/{address}
"""
                    await self.send_alert(message.strip())
                    self.last_alert_time[address] = current_time
                    logger.info(f"[Whale] Rapid change: {format_address(address)} {direction} {format_balance(abs(change))} HYPE in {time_span:.0f}min")

        self._save_known_whales()

    async def monitor(self):
        """Whale monitoring loop"""
        logger.info("[Whale] Starting HYPE whale monitor")
        logger.info(f"[Whale] Min balance: {format_balance(self.min_balance)} HYPE")
        logger.info(f"[Whale] Poll interval: {self.poll_interval}s")

        async with aiohttp.ClientSession() as session:
            await self.fetch_exchange_addresses(session)

            while True:
                try:
                    holders = await self.fetch_top_holders(session)

                    if holders:
                        logger.info(f"[Whale] Checking {len(holders)} holders...")
                        await self.check_for_new_whales(holders)

                    # Refresh exchange list every 10 polls
                    if int(time.time()) % (self.poll_interval * 10) < self.poll_interval:
                        await self.fetch_exchange_addresses(session)

                except Exception as e:
                    logger.error(f"[Whale] Error: {e}")

                await asyncio.sleep(self.poll_interval)


async def main():
    """Entry point"""
    logger.info("=" * 50)
    logger.info("Hyperliquid Monitor")
    logger.info("=" * 50)

    try:
        config = load_config()

        tasks = []

        # Start wallet monitor if wallets configured
        if config.get('wallets'):
            logger.info(f"Loaded {len(config['wallets'])} wallet(s) from config")
            wallet_monitor = HyperliquidMonitor(config)
            tasks.append(wallet_monitor.monitor())
        else:
            logger.warning("No wallets configured - skipping wallet monitor")

        # Start whale monitor if enabled
        if config.get('whale_monitor', {}).get('enabled', True):
            whale_monitor = WhaleMonitor(config)
            tasks.append(whale_monitor.monitor())
        else:
            logger.info("Whale monitor disabled in config")

        if not tasks:
            logger.error("No monitors to run!")
            return

        # Run all monitors concurrently
        await asyncio.gather(*tasks)

    except FileNotFoundError:
        logger.error("Please create config.json from config.example.json")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
