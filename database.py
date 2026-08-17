import sqlite3

DB_PATH = "states.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS states (
            symbol TEXT PRIMARY KEY,
            state TEXT,
            reference_sma REAL,
            target_price REAL
        )
    ''')
    conn.commit()
    conn.close()

def get_state(symbol):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT state, reference_sma, target_price FROM states WHERE symbol = ?", (symbol,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"state": row[0], "reference_sma": row[1], "target_price": row[2]}
    return {"state": "IDLE", "reference_sma": 0.0, "target_price": 0.0}

def update_state(symbol, state, reference_sma, target_price):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO states (symbol, state, reference_sma, target_price)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            state=excluded.state,
            reference_sma=excluded.reference_sma,
            target_price=excluded.target_price
    ''', (symbol, state, reference_sma, target_price))
    conn.commit()
    conn.close()

def reset_state(symbol):
    update_state(symbol, "IDLE", 0.0, 0.0)

def clean_old_states(active_symbols):
    """Veritabanında olup Binance Futures aktif listesinde olmayan eski/çöp sembolleri temizler."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    if active_symbols:
        placeholders = ','.join('?' for _ in active_symbols)
        cursor.execute(f"DELETE FROM states WHERE symbol NOT IN ({placeholders})", [s.upper() + ".P" for s in active_symbols])
    conn.commit()
    conn.close()
