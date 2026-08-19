import json
import logging
import asyncio
import sqlite3
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger("telegram_bot")

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

def get_main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Takip Edilen Coinler", callback_data="menu_tracked")],
        [InlineKeyboardButton(text="📈 SMA Periyodu Değiştir", callback_data="menu_sma")],
        [InlineKeyboardButton(text="🎯 Tepki Yüzdesi Değiştir", callback_data="menu_percent")],
        [InlineKeyboardButton(text="⏱ Zaman Dilimi Değiştir", callback_data="menu_timeframe")],
        [InlineKeyboardButton(text="⏳ Zaman Aşımı Değiştir", callback_data="menu_timeout")],
        [InlineKeyboardButton(text="🔄 Güncel Ayarları Göster", callback_data="menu_refresh")]
    ])

def get_sma_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="10", callback_data="setsma_10"),
         InlineKeyboardButton(text="20", callback_data="setsma_20"),
         InlineKeyboardButton(text="50", callback_data="setsma_50")],
        [InlineKeyboardButton(text="100", callback_data="setsma_100"),
         InlineKeyboardButton(text="200", callback_data="setsma_200")],
        [InlineKeyboardButton(text="◀️ Geri Dön", callback_data="menu_main")]
    ])

def get_percent_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="%0.05", callback_data="setpct_0.05"),
         InlineKeyboardButton(text="%0.1", callback_data="setpct_0.1")],
        [InlineKeyboardButton(text="%0.5", callback_data="setpct_0.5"),
         InlineKeyboardButton(text="%1.0", callback_data="setpct_1.0"),
         InlineKeyboardButton(text="%5.0", callback_data="setpct_5.0")],
        [InlineKeyboardButton(text="◀️ Geri Dön", callback_data="menu_main")]
    ])

def get_timeframe_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1m", callback_data="settf_1m"),
         InlineKeyboardButton(text="5m", callback_data="settf_5m"),
         InlineKeyboardButton(text="15m", callback_data="settf_15m")],
        [InlineKeyboardButton(text="1h", callback_data="settf_1h"),
         InlineKeyboardButton(text="4h", callback_data="settf_4h")],
        [InlineKeyboardButton(text="◀️ Geri Dön", callback_data="menu_main")]
    ])

def get_timeout_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5 dk", callback_data="settime_5"),
         InlineKeyboardButton(text="15 dk", callback_data="settime_15"),
         InlineKeyboardButton(text="30 dk", callback_data="settime_30")],
        [InlineKeyboardButton(text="1 Saat", callback_data="settime_60"),
         InlineKeyboardButton(text="Sınırsız", callback_data="settime_0")],
        [InlineKeyboardButton(text="◀️ Geri Dön", callback_data="menu_main")]
    ])

class TelegramNotifier:
    def __init__(self, config, restart_event):
        self.config = config
        self.restart_event = restart_event
        self.token = config.get("telegram_token", "")
        self.chat_id = config.get("telegram_chat_id", "")
        
        if not self.token or not self.chat_id:
            logger.warning("Telegram token veya chat_id eksik! Bot başlatılmayacak.")
            self.bot = None
            return

        self.bot = Bot(token=self.token)
        self.dp = Dispatcher()
        self.setup_handlers()

    def _get_ayarlar_text(self):
        conf = self._load_config()
        import datetime
        now = datetime.datetime.now().strftime("%H:%M:%S")
        timeout_text = "Sınırsız" if conf.get('timeout_minutes', 0) == 0 else f"{conf.get('timeout_minutes', 0)} dk"
        return (
            "⚙️ *Sistem Kontrol Paneli*\n\n"
            f"📈 *SMA Periyodu:* {conf['sma_period']}\n"
            f"⏱ *Zaman Dilimi:* {conf['timeframe']}\n"
            f"🎯 *Hedef Tepki:* %{conf['reaction_percentage']}\n"
            f"⏳ *Zaman Aşımı:* {timeout_text}\n"
            f"🟢 *LONG Aktif:* {'Açık' if conf['long_enabled'] else 'Kapalı'}\n"
            f"🔴 *SHORT Aktif:* {'Açık' if conf['short_enabled'] else 'Kapalı'}\n\n"
            f"🕒 _Son Güncelleme: {now}_\n\n"
            "Aşağıdaki butonları kullanarak ayarları değiştirebilir veya takip listesini görebilirsiniz:"
        )

    def _get_tracked_coins_text(self):
        conn = sqlite3.connect("states.db")
        cursor = conn.cursor()
        cursor.execute("SELECT symbol, state, target_price FROM states WHERE state != 'IDLE' ORDER BY symbol ASC")
        rows = cursor.fetchall()
        conn.close()
        
        if not rows:
            return "📊 *Takip Edilen Coin Yok*\n\nŞu an SMA kesişimi yapmış ve hedefine ulaşmayı bekleyen aktif bir coin bulunmuyor."
            
        text = f"📊 *Aktif Takip Listesi ({len(rows)} coin)*\n\n"
        limit = 20
        for row in rows[:limit]:
            symbol, state, target = row
            direction = "🟢 LONG" if state == "TOUCHED_LONG" else "🔴 SHORT"
            text += f"▪️ {direction} | *{symbol}* ➔ Hedef: `{format_price(target)}`\n"
            
        if len(rows) > limit:
            text += f"\n_...ve {len(rows) - limit} coin daha sırada._"
            
        return text

    def setup_handlers(self):
        @self.dp.message(Command("ayarlar"))
        async def cmd_ayarlar(message: Message):
            await message.reply(self._get_ayarlar_text(), parse_mode="Markdown", reply_markup=get_main_keyboard())

        @self.dp.message(Command("debug_db"))
        async def cmd_debug_db(message: Message):
            parts = message.text.split()
            if len(parts) < 2:
                await message.reply("Kullanım: /debug_db <SYMBOL> (Örn: /debug_db SPACEUSDT.P)")
                return
            
            symbol = parts[1].upper()
            conn = sqlite3.connect("states.db")
            cursor = conn.cursor()
            cursor.execute("SELECT state, reference_sma, target_price, timestamp FROM states WHERE symbol = ?", (symbol,))
            row = cursor.fetchone()
            conn.close()
            
            if row:
                import time
                state, ref, target, ts = row
                elapsed = time.time() - ts if ts > 0 else 0
                await message.reply(f"📊 *{symbol} Veritabanı Durumu:*\n\n"
                                    f"Durum: `{state}`\n"
                                    f"Referans SMA: `{ref}`\n"
                                    f"Hedef: `{target}`\n"
                                    f"Timestamp: `{ts}` (Şu anki zamana göre {elapsed/60:.1f} dakika önce)", parse_mode="Markdown")
            else:
                await message.reply(f"{symbol} için veritabanında kayıt bulunamadı.")

        @self.dp.callback_query(F.data.startswith("menu_"))
        async def on_menu_click(callback: CallbackQuery):
            action = callback.data.split("_")[1]
            if action == "main" or action == "refresh":
                await callback.message.edit_text(self._get_ayarlar_text(), parse_mode="Markdown", reply_markup=get_main_keyboard())
            elif action == "sma":
                await callback.message.edit_text("📈 *Yeni SMA Periyodunu Seçin:*", parse_mode="Markdown", reply_markup=get_sma_keyboard())
            elif action == "percent":
                await callback.message.edit_text("🎯 *Yeni Hedef Yüzdesini Seçin:*", parse_mode="Markdown", reply_markup=get_percent_keyboard())
            elif action == "timeframe":
                await callback.message.edit_text("⏱ *Yeni Zaman Dilimini Seçin:*", parse_mode="Markdown", reply_markup=get_timeframe_keyboard())
            elif action == "timeout":
                await callback.message.edit_text("⏳ *Zaman Aşımı Süresini Seçin:*", parse_mode="Markdown", reply_markup=get_timeout_keyboard())
            elif action == "tracked":
                await callback.message.edit_text(self._get_tracked_coins_text(), parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ Geri Dön", callback_data="menu_main")]
                ]))
            await callback.answer()

        @self.dp.callback_query(F.data.startswith("set"))
        async def on_set_click(callback: CallbackQuery):
            parts = callback.data.split("_")
            setting_type = parts[0]
            val = parts[1]
            
            if setting_type == "setsma":
                self._update_config("sma_period", int(val))
            elif setting_type == "setpct":
                self._update_config("reaction_percentage", float(val))
            elif setting_type == "settf":
                self._update_config("timeframe", val)
            elif setting_type == "settime":
                self._update_config("timeout_minutes", int(val))
                val = f"{val} dk" if int(val) > 0 else "Sınırsız"
                
            await callback.answer(f"Ayarlar güncellendi: {val}", show_alert=True)
            await callback.message.edit_text(f"✅ Ayarlar güncellendi! Sistem *{val}* ile arka planda otomatik yeniden başlatıldı.\n\n" + self._get_ayarlar_text(), parse_mode="Markdown", reply_markup=get_main_keyboard())
            self.restart_event.set()

    def _load_config(self):
        with open("config.json", "r") as f:
            return json.load(f)

    def _update_config(self, key, value):
        conf = self._load_config()
        conf[key] = value
        with open("config.json", "w") as f:
            json.dump(conf, f, indent=2)

    async def send_alert(self, text):
        if self.bot and self.chat_id:
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=text, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Telegram mesajı gönderilemedi: {e}")

    async def run(self):
        if not self.bot:
            return
        logger.info("Telegram Bot dinlemeye başladı...")
        try:
            await self.dp.start_polling(self.bot)
        except asyncio.CancelledError:
            logger.info("Telegram Bot durduruluyor...")
