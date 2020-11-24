import schedule
import time
import asyncio
import json
import numpy as np
from utils.broker import AlpacaClient
from utils.options_scraper import Scraper, OptionEntry
from dotenv import load_dotenv
from typing import List
import datetime as dt
import arrow
from dataclasses import asdict
from collections import Counter

load_dotenv()
TOP_N = 50
CALL_COUNT = 2
MIN_PREM = 20000
MAX_PREM = 1000000
MAX_DAYS_EXP = 7
TARGET_SIZE = 0.1
LEVERAGE = 2
SPY_EMA_MOVING = 13

# await async functions
complete = lambda f: asyncio.get_event_loop().run_until_complete(f)

alpaca = AlpacaClient()
scraper = Scraper()
complete(scraper.login())

# keep track of options already seen
options_hashset = []

# keep track of frequency of symbol and options
ticker_counter = Counter()
calls_counter = Counter()


def get_new(options: List[OptionEntry]):
    hashes = [hash(frozenset(asdict(option).items())) for option in options]
    hashes = [h for h in hashes if h not in options_hashset]

    new_options = [
        options[idx] for idx, h in enumerate(hashes) if h not in options_hashset
    ]
    options_hashset.extend(hashes)

    return new_options


def get_spy_moving_avg(n=SPY_EMA_MOVING):
    quotes = alpaca.api.get_barset(["SPY"], "1D", limit=20)
    closes = [quote.c for quote in quotes["SPY"]]

    a = np.array(closes)
    ret = np.cumsum(a, dtype=float)
    ret[n:] = ret[n:] - ret[:-n]
    avgs = ret[n - 1 :] / n

    return avgs[-1]


def parse_expiry(exp: str) -> str:
    if exp[2] == "/":
        s = [int(x) for x in exp.split("/")]
        ds = f"20{s[2]}-{s[0]:02}-{s[1]:02}"
        return arrow.get(ds).format("YYYY-MM-DD")

    return arrow.get(exp).format("YYYY-MM-DD")


def trade_on_signals():
    """
    Fetch options and check for new positions
    then wait 30s and repeat. Loop ends when market closes.
    Function gets retriggered everyday.
    """
    spy_ema = get_spy_moving_avg()
    today = arrow.now().format("YYYY-MM-DD")

    while not alpaca.is_market_about_to_close():
        options = complete(scraper.get_options())
        new_options = get_new(options)
        print(f"{len(new_options)} new options")

        for option in new_options:
            # not tradable
            if option.symbol not in alpaca.tradable_assets:
                continue

            ticker_counter[option.symbol] += 1
            counts = ticker_counter.most_common(TOP_N)

            # count calls and puts
            inc = 1 if option.side == "CALLS" else -1
            calls_counter[option.symbol] += inc

            # not in top n tickers
            if option.symbol not in [x[0] for x in counts]:
                continue

            if calls_counter[option.symbol] < CALL_COUNT:
                continue

            if option.premium > MAX_PREM or option.premium < MIN_PREM:
                continue

            e = parse_expiry(option.expiration)
            e = dt.date(*[int(x) for x in e.split("-")])
            s = dt.date(*[int(x) for x in today.split("-")])

            # expiry too far out
            days_to_expiry = np.busday_count(s, e)
            if days_to_expiry > MAX_DAYS_EXP:
                continue

            # SPY is under the EMA
            if alpaca.get_price("SPY") < spy_ema:
                continue

            # calculate position size
            act = alpaca.api.get_account()
            equity = float(act.equity)
            qty = max(1, int(TARGET_SIZE * equity / option.spot))
            pos_value = qty * option.spot

            if pos_value > float(act.cash) * LEVERAGE:
                print(f"cannot afford {qty} {option.symbol}")
                continue

            # enter position
            print(f"submiting buy order for {qty} {option.symbol}")
            alpaca.api.submit_order(option.symbol, qty, "buy", "market", "day")
            time.sleep(0.5)

        time.sleep(30)

    # check for positions that need to be sold
    alpaca.sell_all_positions()


schedule.every().day.at("09:45").do(trade_on_signals)
while True:
    schedule.run_pending()
    time.sleep(60)
