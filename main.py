import json
import asyncio
import logging
from database import init_db
from binance_ws import BinanceTracker
from telegram_bot import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("main")

import os
from aiohttp import web

async def health_check(request):
    return web.Response(text="Bot is running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get('/', health_check)])
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    logger.info(f"Web server started on port {port}")

async def self_ping():
    import aiohttp
    url = "https://crypto-tracker-e21v.onrender.com/"
    while True:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    logger.info(f"[PING] Self-ping attı: HTTP {response.status}")
        except Exception as e:
            logger.error(f"[PING] Self-ping hatası: {e}")
        # Her 5 dakikada bir (300 saniye) ping at
        await asyncio.sleep(300)

def load_config():
    with open("config.json", "r") as f:
        return json.load(f)

async def main():
    try:
        config = load_config()
    except FileNotFoundError:
        logger.error("config.json dosyası bulunamadı! Lütfen oluşturun.")
        return
    except json.JSONDecodeError:
        logger.error("config.json formatı hatalı!")
        return
        
    print(f"Sistem başlatıldı. Ayarlar: SMA {config['sma_period']}, {config['timeframe']}, Hedef: %{config['reaction_percentage']}")
    
    init_db()
    
    # Start fake web server for Render
    asyncio.create_task(start_web_server())
    
    # Uyku modunu engellemek için self-ping başlat
    asyncio.create_task(self_ping())
    
    restart_event = asyncio.Event()
    
    # Telegram bot başlatılır
    tg_bot = TelegramNotifier(config, restart_event)
    tg_task = asyncio.create_task(tg_bot.run())
    
    while True:
        # Config'i her restart döngüsünde yeniden oku
        config = load_config()
        
        tracker = BinanceTracker(config, tg_bot)
        tracker_task = asyncio.create_task(tracker.run())
        restart_task = asyncio.create_task(restart_event.wait())
        
        # Yeniden başlatma sinyali VEYA tracker'ın çökmesini bekle
        done, pending = await asyncio.wait(
            [restart_task, tracker_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        if tracker_task in done:
            logger.error("Tracker beklenmedik şekilde durdu! 10 saniye sonra yeniden başlatılacak...")
            await asyncio.sleep(10)
        else:
            logger.info("Ayar değişikliği algılandı. Tracker yeniden başlatılıyor...")
            
        restart_event.clear()
        
        # Eski tracker'ı durdur
        tracker.stop()
        
        # Tracker'ın kapanmasını bekle
        try:
            await asyncio.wait_for(tracker_task, timeout=5.0)
        except asyncio.TimeoutError:
            tracker_task.cancel()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nSistem kapatılıyor...")
