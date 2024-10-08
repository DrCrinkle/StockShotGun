
<h1 align="center">StockShotGun</h1>
<p align="center">
  A one click solution to submitting orders to multiple brokers at the same time
</p>

## About The Project
I partake in [Reverse Split Arbitrage](https://www.reversesplitarbitrage.com/) and wanted to semi-automate the buying and selling of tickers that were going through a reverse split instead of scrambling around each brokerage to get orders in manually.

## Current Broker Support
* **Tradier**: requires access token
* **Robinhood**: requires username, password and MFA setup token
* **TastyTrade**: requires username and password
* **Public**: requires username and password
* **Firstrade**: requires username, password and PIN
* **Fennel**: requires email
* **Schwab**: requires api key and secret
* **dSPAC**: requires email and password
* **BBAE Pro**: requires email and password

## Getting Started
First you will need to set up authentication
```
git clone 
py -m pip install -r requirements.txt
py main.py setup 
```
The set up will ask for your API keys or credentials and add them to a ```.env``` file

## Usage
To buy a ticker at market
```
py main.py buy 1 TSLA 
```
To sell a ticker at market
```
py main.py sell 1 TSLA 
```
To make a limit order, add a price after the ticker
```
py main.py buy 1 TSLA 650.45
```

## Special Thanks
* [NelsonDane](https://github.com/NelsonDane/)
  * [public-invest-api](https://github.com/NelsonDane/public-invest-api)
  * [fennel-invest-api](https://github.com/NelsonDane/fennel-invest-api)

## To Do
* Add encryption to credentials
* Fully automate by tracking FINRA and/or SEC filings
* Add more brokers (Webull)
* Add per trade logging to a CSV
* maybe add a menu with entries for buy sell setup, to avoid having to rerun script after setup.
