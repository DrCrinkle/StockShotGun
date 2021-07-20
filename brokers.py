
def alpacaTrade(side, qty, ticker, price, alpaca):
    try:
        if price is not None:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='limit',
                                time_in_force='day',
                                limit_price=price)
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
        else:
            alpaca.submit_order(symbol=ticker,
                                qty=qty,
                                side=side,
                                type='market',
                                time_in_force='day')
            if side == "buy":
                print(f"Bought {ticker} on Alpaca")
            else:
                print(f"Sold {ticker} on Alpaca")
    except alpaca.rest.APIError:
        print(alpaca.rest.APIError)
        return False

    return True


def robinTrade(side, qty, ticker, price, rh):
    try:
        if side == 'buy':
            if price is not None:
                rh.order_buy_limit(symbol=ticker, quantity=qty, limitPrice=price)
            else:
                rh.order_buy_market(symbol=ticker, quantity=qty)
            print(f"Bought {ticker} on Robinhood")
        else:
            if price is not None:
                rh.order_sell_limit(symbol=ticker, quantity=qty, limitPrice=price)
            else:
                rh.order_sell_market(symbol=ticker, quantity=qty)
            print(f"Sold {ticker} on Robinhood")
    except:
        return False
    return True
