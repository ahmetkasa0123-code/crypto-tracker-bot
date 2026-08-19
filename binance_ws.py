import asyncio
import json
import logging
import aiohttp
import websockets
from database import get_state, update_state, reset_state, clean_old_states
import time

logger = logging.getLogger("binance_ws")

def format_price(price):
    """Fiyatın büyüklüğüne göre dinamik basamak hassasiyeti belirler."""
    if price is None:
        return "0.0"
    try:
        price = float(price)
    except ValueError:
        return str(price)
        
    if price >= 1000:
        return f"{price:.2f}"
    elif price >= 1:
        return f"{price:.4f}"
    elif price >= 0.0001:
        return f"{price:.6f}"
    else:
        return f"{price:.8f}"

class BinanceTracker:
    def __init__(self, config, telegram_bot=None):
        self.config = config
        self.telegram_bot = telegram_bot
        self.scan_all = config.get("scan_all", False)
        self.symbols = [s.lower() for s in config.get("symbols", [])]
        self.sma_period = config["sma_period"]
        self.timeframe = config["timeframe"]
        self.reaction_percentage = config["reaction_percentage"]
        self.long_enabled = config["long_enabled"]
        self.short_enabled = config["short_enabled"]
        
        self.candles = {}
        self.prev_data = {}
        self.is_running = False

    async def get_all_usdt_symbols(self):
        url = "https://fapi.binance.com/fapi/v1/exchangeInfo"
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    data = await response.json()
                    symbols = []
                    for s in data["symbols"]:
                        if s["quoteAsset"] == "USDT" and s["status"] == "TRADING" and s["contractType"] == "PERPETUAL":
                            symbols.append(s["symbol"].lower())
                    return symbols
                else:
                    logger.error("Sembol listesi çekilemedi!")
                    return []

    async def fetch_historical_klines(self, session, symbol, sem):
        async with sem:
            url = "https://fapi.binance.com/fapi/v1/klines"
            limit = self.sma_period + 100
            params = {
                "symbol": symbol.upper(),
                "interval": self.timeframe,
                "limit": limit
            }
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            
            for attempt in range(3):
                try:
                    await asyncio.sleep(0.05)
                    async with session.get(url, params=params, headers=headers) as response:
                        if response.status == 200:
                            data = await response.json()
                            self.candles[symbol] = []
                            for item in data:
                                self.candles[symbol].append({
                                    "t": int(item[0]),
                                    "h": float(item[2]),
                                    "l": float(item[3]),
                                    "c": float(item[4])
                                })
                            self.warmup_state(symbol)
                            return
                        elif response.status in [403, 429]:
                            wait_time = (attempt + 1) * 3
                            logger.warning(f"[{symbol.upper()}] HTTP {response.status} uyarısı. {wait_time} sn bekleniyor... (Deneme {attempt+1}/3)")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(f"[{symbol.upper()}] Geçmiş veri çekilemedi. HTTP {response.status}")
                            return
                except Exception as e:
                    logger.error(f"[{symbol.upper()}] Hata (Deneme {attempt+1}/3): {e}")
                    await asyncio.sleep(2)
            
            logger.error(f"[{symbol.upper()}] Geçmiş veri 3 denemede de çekilemedi. Bu coin taranmayacak.")

    def warmup_state(self, symbol):
        candles = self.candles[symbol]
        n = len(candles)
        if n < self.sma_period + 2:
            return

        state = "IDLE"
        ref_sma = 0.0
        target = 0.0
        state_time = 0.0

        for i in range(self.sma_period, n):
            curr_candle = candles[i]
            prev_candles = candles[i - self.sma_period : i]
            prev_sma = sum(c["c"] for c in prev_candles) / self.sma_period
            prev_price = candles[i - 1]["c"]

            curr_candles = candles[i - self.sma_period + 1 : i + 1]
            curr_sma = sum(c["c"] for c in curr_candles) / self.sma_period
            curr_price = candles[i]["c"]
            curr_high = candles[i]["h"]
            curr_low = candles[i]["l"]

            if state == "IDLE":
                if self.long_enabled and prev_price < prev_sma and curr_high >= curr_sma:
                    state = "TOUCHED_LONG"
                    ref_sma = curr_sma
                    target = ref_sma + (ref_sma * self.reaction_percentage / 100)
                    state_time = curr_candle["t"] / 1000.0
                elif self.short_enabled and prev_price > prev_sma and curr_low <= curr_sma:
                    state = "TOUCHED_SHORT"
                    ref_sma = curr_sma
                    target = ref_sma - (ref_sma * self.reaction_percentage / 100)
                    state_time = curr_candle["t"] / 1000.0
            elif state == "TOUCHED_LONG":
                timeout_minutes = self.config.get("timeout_minutes", 15)
                elapsed_candle = curr_candle["t"] / 1000.0 - state_time
                if elapsed_candle > timeout_minutes * 60:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
                elif curr_high >= target:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
                elif curr_low < curr_sma:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
            elif state == "TOUCHED_SHORT":
                timeout_minutes = self.config.get("timeout_minutes", 15)
                elapsed_candle = curr_candle["t"] / 1000.0 - state_time
                if elapsed_candle > timeout_minutes * 60:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
                elif curr_low <= target:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
                elif curr_high > curr_sma:
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0

        # Son durumu kaydetmeden önce zaman aşımı kontrolü yap
        # Eğer son sinyal çok eskiyse (şimdiki zamana göre), IDLE'a sıfırla
        if state in ("TOUCHED_LONG", "TOUCHED_SHORT"):
            timeout_minutes = self.config.get("timeout_minutes", 15)
            if state_time > 0:
                elapsed = time.time() - state_time
                logger.info(f"[{symbol.upper()}] Geçmiş tarama: sinyal {elapsed/60:.1f} dk önce oluşmuş. Timeout sınırı: {timeout_minutes} dk.")
                if elapsed > timeout_minutes * 60:
                    logger.info(f"[{symbol.upper()}] TIMEOUT → IDLE'a sıfırlandı.")
                    state = "IDLE"
                    ref_sma = 0.0
                    target = 0.0
                    state_time = 0.0
                else:
                    logger.info(f"[{symbol.upper()}] Geçerli sinyal, veritabanına kaydediliyor.")

        update_state(symbol.upper() + ".P", state, ref_sma, target, state_time)

        last_closed_candles = candles[n - 1 - self.sma_period : n - 1]
        last_closed_sma = sum(c["c"] for c in last_closed_candles) / self.sma_period
        
        self.prev_data[symbol]["price"] = candles[n - 1]["c"]
        self.prev_data[symbol]["sma"] = last_closed_sma

    def calculate_sma(self, symbol):
        if not self.candles.get(symbol) or len(self.candles[symbol]) < self.sma_period:
            return None
        total = sum(candle["c"] for candle in self.candles[symbol][-self.sma_period:])
        return total / self.sma_period

    async def process_price(self, symbol, current_price, current_time):
        current_sma = self.calculate_sma(symbol)
        if current_sma is None:
            return

        prev_price = self.prev_data[symbol]["price"]
        prev_sma = self.prev_data[symbol]["sma"]
        
        state_data = get_state(symbol.upper() + ".P")
        current_state = state_data["state"]

        if current_state == "IDLE" and prev_price is not None and prev_sma is not None:
            if self.long_enabled and prev_price < prev_sma and current_price >= current_sma:
                reference_sma = current_sma
                target_price = reference_sma + (reference_sma * self.reaction_percentage / 100)
                update_state(symbol.upper() + ".P", "TOUCHED_LONG", reference_sma, target_price, time.time())
                logger.info(f"[BİLGİ] {symbol.upper() + '.P'} SMA {self.sma_period}'ye (Yukarı) dokundu. Referans: {format_price(reference_sma)}, Hedef: {format_price(target_price)}")

            elif self.short_enabled and prev_price > prev_sma and current_price <= current_sma:
                reference_sma = current_sma
                target_price = reference_sma - (reference_sma * self.reaction_percentage / 100)
                update_state(symbol.upper() + ".P", "TOUCHED_SHORT", reference_sma, target_price, time.time())
                logger.info(f"[BİLGİ] {symbol.upper() + '.P'} SMA {self.sma_period}'ye (Aşağı) dokundu. Referans: {format_price(reference_sma)}, Hedef: {format_price(target_price)}")

        elif current_state == "TOUCHED_LONG":
            timeout_minutes = self.config.get("timeout_minutes", 15)
            state_timestamp = state_data.get("timestamp", 0.0)
            if state_timestamp > 0 and time.time() - state_timestamp > timeout_minutes * 60:
                logger.info(f"[ZAMAN AŞIMI] {symbol.upper() + '.P'} {timeout_minutes} dk içinde hedefe ulaşamadı. İptal edildi.")
                reset_state(symbol.upper() + ".P")
                return
                    
            target = state_data["target_price"]
            if current_price >= target:
                await self.fire_alarm(symbol.upper() + ".P", current_price, state_data["reference_sma"], "LONG")
                reset_state(symbol.upper() + ".P")
            elif current_price < current_sma:
                logger.info(f"[İPTAL] {symbol.upper() + '.P'} tekrar SMA altına indi. Takip sıfırlandı.")
                reset_state(symbol.upper() + ".P")
                
        elif current_state == "TOUCHED_SHORT":
            timeout_minutes = self.config.get("timeout_minutes", 15)
            state_timestamp = state_data.get("timestamp", 0.0)
            if state_timestamp > 0 and time.time() - state_timestamp > timeout_minutes * 60:
                logger.info(f"[ZAMAN AŞIMI] {symbol.upper() + '.P'} {timeout_minutes} dk içinde hedefe ulaşamadı. İptal edildi.")
                reset_state(symbol.upper() + ".P")
                return
                    
            target = state_data["target_price"]
            if current_price <= target:
                await self.fire_alarm(symbol.upper() + ".P", current_price, state_data["reference_sma"], "SHORT")
                reset_state(symbol.upper() + ".P")
            elif current_price > current_sma:
                logger.info(f"[İPTAL] {symbol.upper() + '.P'} tekrar SMA üstüne çıktı. Takip sıfırlandı.")
                reset_state(symbol.upper() + ".P")

        self.prev_data[symbol]["price"] = current_price
        self.prev_data[symbol]["sma"] = current_sma

    async def fire_alarm(self, symbol, current_price, reference_sma, direction):
        msg = (
            f"🚨 *HEDEF GERÇEKLEŞTİ: {symbol.upper()}*\n\n"
            f"📈 *Periyot:* SMA {self.sma_period} ({self.timeframe})\n"
            f"🎯 *Tepki:* %{self.reaction_percentage}\n"
            f"🔄 *Yön:* {direction}\n"
            f"✅ *Gerçekleşen Fiyat:* {format_price(current_price)}"
        )
        print(f"\n=========================================")
        print(msg)
        print(f"=========================================\n")
        
        if self.telegram_bot:
            await self.telegram_bot.send_alert(msg)

    async def listen_to_chunk(self, chunk, chunk_id):
        """Belirli bir parite grubunu kendi WebSocket bağlantısı üzerinden dinler."""
        # Binance'in yeni yönlendirilmiş mimarisine göre Kline verileri /market rotasından çekilir
        streams_path = "/".join(f"{symbol}@kline_{self.timeframe}" for symbol in chunk)
        ws_url = f"wss://fstream.binance.com/market/stream?streams={streams_path}"
        
        while self.is_running:
            try:
                logger.info(f"[WS-{chunk_id}] WebSocket bağlanılıyor...")
                async with websockets.connect(ws_url) as websocket:
                    logger.info(f"[WS-{chunk_id}] Bağlantı başarılı. {len(chunk)} parite dinleniyor...")
                    
                    msg_count = 0
                    first_msg_logged = False
                    async for message in websocket:
                        if not self.is_running:
                            break
                            
                        payload = json.loads(message)
                        
                        if not first_msg_logged:
                            logger.info(f"[WS-{chunk_id}] İlk ham mesaj alındı: {message[:300]}")
                            first_msg_logged = True
                            
                        if "data" in payload:
                            data = payload["data"]
                        else:
                            continue
                            
                        msg_count += 1
                        if msg_count % 100 == 0:
                            logger.info(f"[WS-{chunk_id}] Toplam {msg_count} adet veri paketi başarıyla işlendi.")
                            
                        if "k" in data:
                            kline = data["k"]
                            symbol = data["s"].lower()
                            start_time = kline["t"]
                            close_price = float(kline["c"])
                            
                            if symbol in self.candles and len(self.candles[symbol]) > 0:
                                last_candle = self.candles[symbol][-1]
                                if start_time == last_candle["t"]:
                                    last_candle["c"] = close_price
                                    last_candle["h"] = float(kline["h"])
                                    last_candle["l"] = float(kline["l"])
                                elif start_time > last_candle["t"]:
                                    self.candles[symbol].append({
                                        "t": start_time,
                                        "c": close_price,
                                        "h": float(kline["h"]),
                                        "l": float(kline["l"])
                                    })
                                    if len(self.candles[symbol]) > self.sma_period:
                                        self.candles[symbol].pop(0)
                            
                            await self.process_price(symbol, close_price, start_time)
                            
            except websockets.ConnectionClosed:
                if self.is_running:
                    logger.warning(f"[WS-{chunk_id}] Bağlantı koptu. 5 sn içinde yeniden bağlanılıyor...")
                    await asyncio.sleep(5)
            except Exception as e:
                if self.is_running:
                    logger.error(f"[WS-{chunk_id}] Beklenmeyen hata: {e}")
                    await asyncio.sleep(5)

    def stop(self):
        self.is_running = False

    async def run(self):
        self.is_running = True
        if self.scan_all:
            logger.info("Tüm USDT pariteleri aranıyor...")
            self.symbols = await self.get_all_usdt_symbols()
            logger.info(f"Toplam {len(self.symbols)} adet USDT Futures paritesi bulundu.")
            
        clean_old_states(self.symbols)
            
        for sym in self.symbols:
            if sym not in self.candles:
                self.candles[sym] = []
            if sym not in self.prev_data:
                self.prev_data[sym] = {"price": None, "sma": None}

        logger.info(f"{len(self.symbols)} coin için geçmiş veriler indiriliyor ve simüle ediliyor, lütfen bekleyin...")
        sem = asyncio.Semaphore(10)
        async with aiohttp.ClientSession() as session:
            tasks = [self.fetch_historical_klines(session, symbol, sem) for symbol in self.symbols]
            await asyncio.gather(*tasks)
            
        logger.info("Geçmiş veriler ve durum simülasyonu tamamlandı. Çoklu WebSocket bağlantıları açılıyor...")
        
        chunk_size = 150
        chunks = [self.symbols[i:i + chunk_size] for i in range(0, len(self.symbols), chunk_size)]
        
        chunk_tasks = [self.listen_to_chunk(chunk, idx + 1) for idx, chunk in enumerate(chunks)]
        await asyncio.gather(*chunk_tasks)
        
        logger.info("Binance Tracker başarıyla durduruldu.")
