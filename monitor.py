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


def load_config() -> dict:
    """Load configuration from config.json"""
    config_path = Path(__file__).parent / "config.json"

    if not config_path.exists():
        logger.error("config.json not found! Copy config.example.json to config.json and fill in your details.")
        raise FileNotFoundError("config.json not found")

    with open(config_path, 'r') as f:
        return json.load(f)


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

        # Track processed fills to avoid duplicates
        self.processed_fills = set()

        # Store start time to ignore historical fills (in milliseconds)
        self.start_time = int(time.time() * 1000)

        # TWAP detection: track fill counts per wallet+coin+type
        # Key: "{address}_{coin}_{open/close}", Value: {"count": int, "last_time": int}
        self.fill_series = {}
        self.TWAP_ALERT_LIMIT = 3  # Max alerts before assuming TWAP
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

        # Check if we should send alert (only first 5 in series)
        count = self.fill_series[series_key]["count"]
        if count > self.TWAP_ALERT_LIMIT:
            # Send TWAP notification only on the 6th fill
            if count == self.TWAP_ALERT_LIMIT + 1:
                label = self.wallets.get(address.lower(), format_address(address))
                twap_msg = f"⏳ <b>{label}</b> - TWAP detected\n\n<b>Coin:</b> {coin}\nMuting further alerts until 5 min pause..."
                await self.send_alert(twap_msg)
            logger.info(f"TWAP detected, skipping alert: {coin} {fill_type} (fill #{count})")
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


async def main():
    """Entry point"""
    logger.info("=" * 50)
    logger.info("Hyperliquid Wallet Monitor")
    logger.info("=" * 50)

    try:
        config = load_config()

        if not config.get('wallets'):
            logger.error("No wallets configured! Add wallets to config.json")
            return

        logger.info(f"Loaded {len(config['wallets'])} wallet(s) from config")

        monitor = HyperliquidMonitor(config)
        await monitor.monitor()

    except FileNotFoundError:
        logger.error("Please create config.json from config.example.json")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
