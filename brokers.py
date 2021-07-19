
def alpacaTrade(side, qty, ticker, price, alpaca):
    try:
        if price is not None:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='limit',
                                time_in_force='day',
                                limit_price=price)
            print(f"Bought {ticker} on Alpaca")
        else:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='market',
                                time_in_force='day')
            print(f"Bought {ticker} on Alpaca")
    except alpaca.rest.APIError:
        print(alpaca.rest.APIError)
        return False

    return True

#def robinTrade(side, qty, ticker, price):
