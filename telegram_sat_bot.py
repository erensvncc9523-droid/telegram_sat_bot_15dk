from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
# Reuse the BIST list and daily signal logic from the existing scanner.
from tarama import BIST_HISSELER, sinyal_hesapla, veri_cek_kaynakli


def get_data_dir() -> Path:
    data_dir = Path(os.getenv("BOT_DATA_DIR", "."))
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir
    except OSError:
        fallback = Path(".")
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = get_data_dir()
CONFIG_PATH = Path("telegram_bot_config.json")
STATE_PATH = DATA_DIR / "telegram_bot_state.json"
LOG_PATH = DATA_DIR / "telegram_sat_bot.log"
NETWORK_RETRY_COUNT = 3
NETWORK_RETRY_DELAY_SECONDS = 10
RUN_WINDOW_TZ = "Europe/Istanbul"
RUN_WINDOW_START = "10:15"
RUN_WINDOW_END = "18:30"

DEFAULT_CONFIG = {
    "telegram_bot_token": "BURAYA_BOT_TOKEN",
    "telegram_chat_id": "BURAYA_CHAT_ID",
    "symbols": BIST_HISSELER,
    "scan_interval_minutes": 15,
    "history_days_daily": 220,
    "history_days_sell": 220,
    "buy_timeframe": "1d",
    "sell_timeframe": "1d",
    "use_trend": False,
    "ma_trend_len": 200,
    "use_htf": False,
    "ma_htf_len": 200,
    "use_volume": False,
    "use_confirm": True,
    "med_len": 3,
    "rsi_len": 14,
    "stoch_len": 14,
    "smooth_k": 3,
    "smooth_d": 3,
    "ema_len": 14,
    "lookback": 4,
    "volume_len": 20,
    "send_buy_messages": False,
    "send_sell_messages": True,
    "send_scan_summary": True,
    "send_signal_message_separately": False,
    "timezone_note": "Saatler veri kaynagina gore degisebilir."
}


def parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text == "":
        return default
    return text not in {"0", "false", "hayir", "no", "off"}


def parse_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_hhmm(value: str) -> tuple[int, int]:
    hour_text, minute_text = value.strip().split(":", 1)
    return int(hour_text), int(minute_text)


def is_within_run_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(ZoneInfo(RUN_WINDOW_TZ))
    start_hour, start_minute = parse_hhmm(os.getenv("RUN_WINDOW_START", RUN_WINDOW_START))
    end_hour, end_minute = parse_hhmm(os.getenv("RUN_WINDOW_END", RUN_WINDOW_END))
    current_minutes = now.hour * 60 + now.minute
    start_minutes = start_hour * 60 + start_minute
    end_minutes = end_hour * 60 + end_minute
    return start_minutes <= current_minutes <= end_minutes


def apply_env_overrides(config: dict) -> dict:
    env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    env_symbols = os.getenv("SYMBOLS", "").strip()

    if env_token:
        config["telegram_bot_token"] = env_token
    if env_chat_id:
        config["telegram_chat_id"] = env_chat_id
    if env_symbols:
        config["symbols"] = [part.strip().upper() for part in env_symbols.split(",") if part.strip()]

    int_envs = {
        "SCAN_INTERVAL_MINUTES": "scan_interval_minutes",
        "HISTORY_DAYS_DAILY": "history_days_daily",
        "HISTORY_DAYS_SELL": "history_days_sell",
    }
    for env_name, key in int_envs.items():
        if os.getenv(env_name) is not None:
            config[key] = parse_int(os.getenv(env_name), int(config.get(key, DEFAULT_CONFIG[key])))

    text_envs = {
        "BUY_TIMEFRAME": "buy_timeframe",
        "SELL_TIMEFRAME": "sell_timeframe",
    }
    for env_name, key in text_envs.items():
        value = os.getenv(env_name, "").strip()
        if value:
            config[key] = value

    bool_envs = {
        "SEND_BUY_MESSAGES": "send_buy_messages",
        "SEND_SELL_MESSAGES": "send_sell_messages",
        "SEND_SCAN_SUMMARY": "send_scan_summary",
        "SEND_SIGNAL_MESSAGE_SEPARATELY": "send_signal_message_separately",
    }
    for env_name, key in bool_envs.items():
        if os.getenv(env_name) is not None:
            config[key] = parse_bool(os.getenv(env_name), bool(config.get(key, DEFAULT_CONFIG[key])))

    return config


@dataclass
class Event:
    timestamp: pd.Timestamp
    kind: str
    price: float
    reason: str = ""


@dataclass
class StrategyResult:
    events: list[Event]
    last_price: float | None
    change_pct: float | None
    data_source: str = ""


def configure_logging() -> None:
    handlers: list[logging.Handler] = []
    try:
        handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    except OSError:
        handlers.append(logging.StreamHandler(sys.stdout))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        handlers=handlers,
        force=True,
    )


def log_info(message: str) -> None:
    print(message, flush=True)
    logging.info(message)


def log_error(message: str) -> None:
    print(message, flush=True)
    logging.error(message)


def ensure_config() -> dict:
    config = dict(DEFAULT_CONFIG)

    # Railway/cloud deployments normally use Variables instead of a local JSON file.
    if os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_CHAT_ID"):
        return apply_env_overrides(config)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2, ensure_ascii=False), encoding="utf-8")
        log_info(f"Config created: {CONFIG_PATH}")
        log_info("Telegram bot token ve chat id alanlarini doldurup tekrar calistirin.")
        sys.exit(0)

    config.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    if "history_days_sell" not in config and "history_days_4h" in config:
        config["history_days_sell"] = config["history_days_4h"]
    return apply_env_overrides(config)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sent_events": {}, "last_scan": None, "closed_symbols": []}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state.setdefault("sent_events", {})
        state.setdefault("last_scan", None)
        state.setdefault("closed_symbols", [])
        return state
    except json.JSONDecodeError:
        return {"sent_events": {}, "last_scan": None, "closed_symbols": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> None:
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    last_error = None
    for attempt in range(1, NETWORK_RETRY_COUNT + 1):
        try:
            request = urllib.request.Request(url, data=payload, method="POST")
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            if not parsed.get("ok", False):
                raise RuntimeError(f"Telegram API error: {parsed}")
            return
        except Exception as exc:
            last_error = exc
            if attempt < NETWORK_RETRY_COUNT:
                time.sleep(NETWORK_RETRY_DELAY_SECONDS)
    raise last_error


# Indicator helpers

def percentile_nearest_rank(series: pd.Series, length: int, pct: int) -> pd.Series:
    result = pd.Series(np.nan, index=series.index, dtype="float64")
    values = series.to_numpy(dtype=float)
    for i in range(length - 1, len(values)):
        window = values[i - length + 1 : i + 1]
        window = window[~np.isnan(window)]
        if len(window) == 0:
            continue
        idx = int(math.ceil(pct / 100.0 * len(window))) - 1
        idx = max(0, min(idx, len(window) - 1))
        result.iloc[i] = np.sort(window)[idx]
    return result


def sma(series: pd.Series, length: int) -> pd.Series:
    return series.rolling(window=length).mean()


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi_calc(close: pd.Series, length: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def stoch_rsi(close: pd.Series, rsi_len: int, stoch_len: int, smooth_k: int, smooth_d: int) -> tuple[pd.Series, pd.Series]:
    rsi_val = rsi_calc(close, rsi_len)
    rsi_min = rsi_val.rolling(stoch_len).min()
    rsi_max = rsi_val.rolling(stoch_len).max()
    stoch_raw = (rsi_val - rsi_min) / (rsi_max - rsi_min + 1e-10) * 100
    k_line = sma(stoch_raw, smooth_k)
    d_line = sma(k_line, smooth_d)
    return k_line, d_line


def crossover(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    return (series_a > series_b) & (series_a.shift(1) <= series_b.shift(1))


def crossunder(series_a: pd.Series, series_b: pd.Series) -> pd.Series:
    return (series_a < series_b) & (series_a.shift(1) >= series_b.shift(1))


def crossover_window(series_a: pd.Series, series_b: pd.Series, lookback: int) -> pd.Series:
    return crossover(series_a, series_b).rolling(lookback).max().fillna(0).astype(bool)


def download_ohlcv(ticker: str, period: str, interval: str) -> tuple[pd.DataFrame | None, str]:
    last_error = None
    for attempt in range(1, NETWORK_RETRY_COUNT + 1):
        try:
            df, data_source = veri_cek_kaynakli(ticker, period, interval)
            if df is None or df.empty:
                return None, ""
            if getattr(df.index, "tz", None) is not None:
                df.index = df.index.tz_convert(None)
            return df.dropna(subset=["Open", "High", "Low", "Close"]), data_source
        except Exception as exc:
            last_error = exc
            if attempt < NETWORK_RETRY_COUNT:
                time.sleep(NETWORK_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    return None, ""


def ensure_series(data: pd.Series | pd.DataFrame) -> pd.Series:
    if isinstance(data, pd.DataFrame):
        if data.shape[1] == 0:
            return pd.Series(dtype="float64")
        data = data.iloc[:, 0]
    return pd.Series(data).astype("float64")


def latest_price_change(df: pd.DataFrame) -> tuple[float | None, float | None]:
    close = ensure_series(df["Close"]).dropna()
    if len(close) == 0:
        return None, None
    last_price = float(close.iloc[-1])
    if len(close) < 2:
        return last_price, None
    prev_price = float(close.iloc[-2])
    change_pct = (last_price / prev_price - 1.0) * 100.0 if prev_price > 0 else None
    return last_price, change_pct


def compute_daily_strategy_events(df: pd.DataFrame) -> list[Event]:
    al_sinyal, sat_sinyal, close, grade, stop_fiyat, sat_neden = sinyal_hesapla(df)
    events = []

    for ts in df.index[al_sinyal.fillna(False)]:
        events.append(Event(timestamp=pd.Timestamp(ts), kind="BUY", price=float(close.loc[ts])))
    for ts in df.index[sat_sinyal.fillna(False)]:
        reason = sat_neden.loc[ts] if sat_neden.loc[ts] else "SAT"
        events.append(Event(timestamp=pd.Timestamp(ts), kind="SELL", price=float(close.loc[ts]), reason=reason))

    return sorted(events, key=lambda item: (item.timestamp, 0 if item.kind == "BUY" else 1))


def build_strategy_result(symbol: str, config: dict) -> StrategyResult:
    ticker = f"{symbol}.IS"
    daily_df, data_source = download_ohlcv(ticker, f"{config['history_days_daily']}d", config["buy_timeframe"])
    if daily_df is None:
        return StrategyResult(events=[], last_price=None, change_pct=None, data_source=data_source)
    last_price, change_pct = latest_price_change(daily_df)
    merged = compute_daily_strategy_events(daily_df)

    filtered = []
    in_position = False
    for event in merged:
        if event.kind == "BUY" and not in_position:
            filtered.append(event)
            in_position = True
        elif event.kind == "SELL" and in_position:
            filtered.append(event)
            in_position = False
    return StrategyResult(events=filtered, last_price=last_price, change_pct=change_pct, data_source=data_source)


def price_change_text(result: StrategyResult) -> str:
    if result.last_price is None:
        return "Fiyat: yok"
    if result.change_pct is None:
        return f"Fiyat: {result.last_price:.2f}"
    sign = "+" if result.change_pct >= 0 else ""
    return f"Fiyat: {result.last_price:.2f} ({sign}{result.change_pct:.2f}%)"


def no_signal_text(symbol: str, result: StrategyResult) -> str:
    return "\n".join(
        [
            "--------------------",
            f"📌 Hisse: {symbol}",
            "⚪ Durum: Sinyal yok",
            f"💰 {price_change_text(result)}",
            f"Veri: {result.data_source or 'yok'}",
        ]
    )


def signal_emoji(event_name: str, event_kind: str) -> str:
    if event_kind == "BUY":
        return "🟢"
    if "STOP" in event_name.upper():
        return "🛑"
    return "🔴"


def format_signal_message(symbol: str, event_name: str, event: Event, result: StrategyResult) -> str:
    return "\n".join(
        [
            f"{signal_emoji(event_name, event.kind)} {event_name} SİNYALİ",
            "",
            "--------------------",
            f"📌 Hisse: {symbol}",
            f"💰 Sinyal Fiyatı: {event.price:.2f}",
            f"📊 Son Durum: {price_change_text(result)}",
            f"🕒 Sinyal Zamanı: {event.timestamp.strftime('%Y-%m-%d %H:%M')}",
            f"Veri: {result.data_source or 'yok'}",
            "🧭 Strateji: Günlük AL + günlük SAT",
            "--------------------",
        ]
    )


def format_signal_summary_line(symbol: str, event_name: str, event: Event, result: StrategyResult) -> str:
    return "\n".join(
        [
            "--------------------",
            f"{signal_emoji(event_name, event.kind)} Hisse: {symbol}",
            f"📣 Sinyal: {event_name}",
            f"💰 Sinyal Fiyatı: {event.price:.2f}",
            f"📊 {price_change_text(result)}",
            f"🕒 Zaman: {event.timestamp.strftime('%Y-%m-%d %H:%M')}",
            f"Veri: {result.data_source or 'yok'}",
        ]
    )


def format_error_summary_line(symbol: str) -> str:
    return "\n".join(
        [
            "--------------------",
            f"📌 Hisse: {symbol}",
            "⚠️ Durum: Hata",
        ]
    )


def scan_once(config: dict, state: dict) -> int:
    bot_token = config["telegram_bot_token"].strip()
    chat_id = str(config["telegram_chat_id"]).strip()
    if not bot_token or bot_token == "BURAYA_BOT_TOKEN" or not chat_id or chat_id == "BURAYA_CHAT_ID":
        log_error("telegram_bot_config.json icindeki token/chat_id alanlarini doldurun.")
        return 0

    log_info(
        "Mesaj ayarlari: "
        f"BUY={config.get('send_buy_messages', False)}, "
        f"SELL={config.get('send_sell_messages', True)}, "
        f"OZET={config.get('send_scan_summary', True)}, "
        f"TEKIL={config.get('send_signal_message_separately', False)}"
    )

    sent_events = state.setdefault("sent_events", {})
    closed_symbols = set(state.setdefault("closed_symbols", []))
    messages_sent = 0
    scan_time = datetime.now(ZoneInfo(RUN_WINDOW_TZ)).strftime("%Y-%m-%d %H:%M:%S")
    summary_lines = [
        "📡 Telegram SAT Botu",
        "",
        f"🕒 Tarama zamanı: {scan_time}",
    ]

    symbols = config["symbols"]
    if isinstance(symbols, str):
        symbols = [part.strip().upper() for part in symbols.split(",") if part.strip()]

    for symbol in symbols:
        if symbol in closed_symbols:
            log_info(f"{symbol}: daha once SAT verdigi icin atlandi")
            continue
        try:
            log_info(f"{symbol}: taraniyor...")
            result = build_strategy_result(symbol, config)
            events = result.events
            if not events:
                log_info(f"{symbol}: olay bulunamadi")
                summary_lines.append(no_signal_text(symbol, result))
                continue
            latest_event = events[-1]
            event_name = latest_event.reason if latest_event.kind == "SELL" and latest_event.reason else latest_event.kind
            log_info(f"{symbol}: son olay = {event_name} @ {latest_event.timestamp:%Y-%m-%d %H:%M} fiyat {latest_event.price:.2f}")
            event_key = f"{symbol}|{event_name}|{latest_event.timestamp.isoformat()}"
            if event_key in sent_events:
                log_info(f"{symbol}: bu olay daha once gonderilmis")
                summary_lines.append(no_signal_text(symbol, result))
                continue
            if latest_event.kind == "BUY" and not config["send_buy_messages"]:
                log_info(f"{symbol}: son olay BUY, BUY mesajlari kapali")
                summary_lines.append(no_signal_text(symbol, result))
                continue
            if latest_event.kind == "SELL" and not config["send_sell_messages"]:
                log_info(f"{symbol}: son olay SELL, SELL mesajlari kapali")
                summary_lines.append(no_signal_text(symbol, result))
                continue

            can_send_separate_signal = config.get("send_signal_message_separately", False)
            can_send_summary = config.get("send_scan_summary", True)
            if not can_send_separate_signal and not can_send_summary:
                log_info(
                    f"{symbol}: gonderilecek {event_name} var ama "
                    "SEND_SIGNAL_MESSAGE_SEPARATELY=false ve SEND_SCAN_SUMMARY=false"
                )
                continue

            sent_events[event_key] = scan_time
            if can_send_separate_signal:
                message = format_signal_message(symbol, event_name, latest_event, result)
                send_telegram_message(bot_token, chat_id, message)
                messages_sent += 1
                log_info(f"Mesaj gonderildi: {symbol} {event_name} {latest_event.timestamp}")
            if latest_event.kind == "SELL":
                summary_lines.append(format_signal_summary_line(symbol, event_name, latest_event, result))
                closed_symbols.add(symbol)
            else:
                summary_lines.append(no_signal_text(symbol, result))
        except Exception as exc:
            log_error(f"{symbol} icin hata: {exc}")
            summary_lines.append(format_error_summary_line(symbol))

    state["closed_symbols"] = sorted(closed_symbols)
    if config.get("send_scan_summary", True):
        summary_message = "\n".join(summary_lines)
        try:
            send_telegram_message(bot_token, chat_id, summary_message)
            messages_sent += 1
            log_info(f"Ozet mesaj gonderildi. Mesaj sayisi: {messages_sent}")
        except Exception as exc:
            log_error(f"Telegram ozet mesaji gonderilemedi: {exc}")

    state["last_scan"] = scan_time
    save_state(state)
    if messages_sent == 0:
        log_info("Telegram mesaji gonderilmedi: yeni gonderilecek SELL yok veya mesaj/ozet ayarlari kapali.")
    log_info(f"Tarama tamamlandi. Gonderilen mesaj: {messages_sent}")
    return messages_sent


def main() -> None:
    configure_logging()
    try:
        print("Telegram SAT botu hazirlaniyor...", flush=True)
        if os.getenv("RAILWAY_ENVIRONMENT") and not is_within_run_window():
            now = datetime.now(ZoneInfo(RUN_WINDOW_TZ)).strftime("%Y-%m-%d %H:%M")
            log_info(f"Calisma saati disinda: {now}. Tarama yapilmadan cikiliyor.")
            return
        config = ensure_config()
        state = load_state()
        bot_token = config["telegram_bot_token"].strip()
        chat_id = str(config["telegram_chat_id"]).strip()

        if "--test-telegram" in sys.argv:
            send_telegram_message(bot_token, chat_id, "Test mesaji: Telegram SAT botu baglandi.")
            log_info("Test mesaji gonderildi.")
            return

        run_once = "--once" in sys.argv or "--loop" not in sys.argv
        if run_once:
            log_info("Tek seferlik tarama basladi.")
            count = scan_once(config, state)
            log_info(f"Tarama bitti. Gonderilen mesaj: {count}")
            return

        interval_seconds = int(config.get("scan_interval_minutes", 15)) * 60
        log_info("Telegram SAT botu surekli modda basladi. Durdurmak icin Ctrl+C.")
        while True:
            count = scan_once(config, state)
            log_info(f"Tarama tamamlandi. Gonderilen mesaj: {count}")
            time.sleep(interval_seconds)
    except Exception as exc:
        log_error(f"Bot genel hata ile kapandi: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
