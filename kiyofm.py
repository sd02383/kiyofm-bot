import asyncio
import yfinance as yf
import pandas as pd
import pandas_ta as ta
import os
import requests
import json
from datetime import datetime
from textblob import TextBlob
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# --- Keep-Alive Server for Replit ---
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Kiyofm Bot is alive!"

def run_web_server():
  app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run_web_server)
    t.start()

# --- CONFIGURATION (from Replit Secrets) ---
# Securely load credentials from environment variables (Replit Secrets)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
CHAT_ID = os.environ.get('CHAT_ID')
NEWS_API_KEY = os.environ.get('NEWS_API_KEY')

# Basic validation
if not all([TELEGRAM_TOKEN, CHAT_ID, NEWS_API_KEY]):
    print("FATAL: A required secret (TELEGRAM_TOKEN, CHAT_ID, or NEWS_API_KEY) is missing.")
    print("Please add them in the Secrets tab (padlock icon).")
    exit()

# --- SETTINGS ---
TICKER = "RELIANCE.NS"
TRADE_LOG_FILE = "completed_trades.csv"
STATE_FILE = "trade_state.json" # File to store the current open position

# --- STATE & LOGGING FUNCTIONS ---

def get_trade_state():
    """Reads the current trade state from a JSON file."""
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"open_position": None, "entry_price": 0}

def set_trade_state(state):
    """Writes the current trade state to a JSON file."""
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def log_completed_trade(entry_time, exit_time, trade_type, entry_price, exit_price):
    """Logs a completed trade to the CSV file and calculates P&L."""
    profit_loss = exit_price - entry_price
    profit_loss_percent = (profit_loss / entry_price) * 100
    
    header_needed = not os.path.exists(TRADE_LOG_FILE)
    log_entry = pd.DataFrame([{
        "Entry Time": entry_time,
        "Exit Time": exit_time,
        "Ticker": TICKER,
        "Trade Type": trade_type,
        "Entry Price": f"₹{entry_price:.2f}",
        "Exit Price": f"₹{exit_price:.2f}",
        "Profit/Loss": f"₹{profit_loss:.2f}",
        "P/L %": f"{profit_loss_percent:.2f}%"
    }])
    log_entry.to_csv(TRADE_LOG_FILE, mode='a', header=header_needed, index=False)
    return profit_loss, profit_loss_percent

# --- TRADING & NEWS LOGIC ---

def get_signal_and_price():
    """Fetches stock data and determines a buy/sell signal and the current price."""
    ticker_obj = yf.Ticker(TICKER)
    data = ticker_obj.history(period="2d", interval="15m")
    if data.empty: return None, None
    data.ta.sma(length=5, append=True)
    data.ta.rsi(length=14, append=True)
    data.rename(columns={"SMA_5": "SMA", "RSI_14": "RSI"}, inplace=True)
    if len(data) < 2: return None, None
    last_row = data.iloc[-1]
    previous_row = data.iloc[-2]
    signal = None
    if previous_row["Close"] < previous_row["SMA"] and last_row["Close"] > last_row["SMA"] and last_row["RSI"] < 70:
        signal = "BUY"
    elif previous_row["Close"] > previous_row["SMA"] and last_row["Close"] < last_row["SMA"] and last_row["RSI"] > 30:
        signal = "SELL"
    return signal, last_row['Close']

def get_news_sentiment(stock_ticker):
    """Fetches news and analyzes sentiment."""
    try:
        query = stock_ticker.replace(".NS", "")
        url = f"https://newsapi.org/v2/everything?q={query}&apiKey={NEWS_API_KEY}&language=en&sortBy=publishedAt&pageSize=10"
        response = requests.get(url)
        response.raise_for_status()
        articles = response.json().get("articles", [])
        if not articles: return "Neutral"
        sentiment_polarity = sum(TextBlob(article['title']).sentiment.polarity for article in articles[:5])
        if sentiment_polarity > 0.3: return "Positive"
        if sentiment_polarity < -0.3: return "Negative"
        return "Neutral"
    except Exception:
        return "Neutral"

# --- CORE BOT JOB ---

async def check_trades(context: ContextTypes.DEFAULT_TYPE):
    """The main function that runs periodically to check for trade signals."""
    now_ist = datetime.now()
    market_open = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now_ist.replace(hour=15, minute=30, second=0, microsecond=0)
    
    # Only run during market hours on weekdays
    if not (market_open <= now_ist <= market_close and 0 <= now_ist.weekday() <= 4):
        return

    print(f"\nRunning trade check at {now_ist.strftime('%H:%M:%S')}...")
    signal, price = get_signal_and_price()
    if not signal:
        print("No technical signal found.")
        return

    state = get_trade_state()
    sentiment = get_news_sentiment(TICKER)

    # --- ENTRY LOGIC ---
    if signal == "BUY" and state["open_position"] is None and sentiment != "Negative":
        new_state = {"open_position": "LONG", "entry_price": price, "entry_time": now_ist.isoformat()}
        set_trade_state(new_state)
        message = f"✅ ENTRY: Bought {TICKER} at ₹{price:.2f}. News: {sentiment}."
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        print(message)

    # --- EXIT LOGIC ---
    elif signal == "SELL" and state["open_position"] == "LONG" and sentiment != "Positive":
        profit, percent = log_completed_trade(
            entry_time=state["entry_time"],
            exit_time=now_ist.isoformat(),
            trade_type="LONG",
            entry_price=state["entry_price"],
            exit_price=price
        )
        message = (
            f"❌ EXIT: Sold {TICKER} at ₹{price:.2f}.\n"
            f"   ▶️ Profit/Loss: ₹{profit:.2f} ({percent:.2f}%)\n"
            f"   ▶️ News: {sentiment}."
        )
        await context.bot.send_message(chat_id=CHAT_ID, text=message)
        set_trade_state({"open_position": None, "entry_price": 0}) # Reset state
        print(message)
    else:
        print(f"Signal '{signal}' found, but no action taken (Position: {state['open_position']}, Sentiment: {sentiment}).")


# --- TELEGRAM COMMAND HANDLERS ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /start command."""
    await update.message.reply_text(
        "Hello! I am Kiyofm, your AI Fund Manager.\n"
        "I will automatically check for trades every 15 minutes during market hours.\n"
        "Type /report to get a summary of all completed trades."
    )

async def report(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /report command. Sends a trade summary."""
    try:
        trades_df = pd.read_csv(TRADE_LOG_FILE)
        if trades_df.empty:
            await update.message.reply_text("No completed trades have been logged yet.")
            return

        # Calculate summary stats
        total_trades = len(trades_df)
        trades_df["P/L Value"] = trades_df["Profit/Loss"].replace({r'[₹]': ''}, regex=True).astype(float)
        wins = trades_df[trades_df["P/L Value"] > 0]
        losses = trades_df[trades_df["P/L Value"] <= 0]
        win_rate = (len(wins) / total_trades) * 100 if total_trades > 0 else 0
        total_pnl = trades_df["P/L Value"].sum()

        # Format the report message
        summary_message = (
            f"📊 *Kiyofm Trade Report*\n\n"
            f"*Total Trades:* {total_trades}\n"
            f"*Wins:* {len(wins)} | *Losses:* {len(losses)}\n"
            f"*Win Rate:* {win_rate:.2f}%\n"
            f"*Total P/L:* ₹{total_pnl:.2f}\n\n"
            f"Sending the full trade log as a file..."
        )
        await update.message.reply_text(summary_message, parse_mode='Markdown')
        
        # Send the CSV file
        await context.bot.send_document(chat_id=update.effective_chat.id, document=open(TRADE_LOG_FILE, 'rb'))

    except FileNotFoundError:
        await update.message.reply_text("No trade report file found. Complete a trade to generate one.")
    except Exception as e:
        await update.message.reply_text(f"An error occurred while generating the report: {e}")


# --- MAIN BOT SETUP ---

def main():
    """Starts the bot, the web server, and the trading job."""
    
    # Start the keep-alive web server
    keep_alive()

    # Create the Telegram Bot Application
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Add command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("report", report))

    # Add the periodic job to check for trades
    job_queue = application.job_queue
    job_queue.run_repeating(check_trades, interval=900, first=10) # 900 seconds = 15 minutes

    print("Bot started! Listening for commands and checking for trades...")
    
    # Start the bot
    application.run_polling()

if __name__ == "__main__":
    main()
