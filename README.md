
<h1 align="center">StockShotGun</h1>
<p align="center">
  A one click solution to submitting orders to multiple brokers at the same time
</p>

## About The Project
I partake in [Reverse Split Arbitrage](https://www.reversesplitarbitrage.com/) and wanted to semi-automate the buying and selling of tickers that were going through a reverse split instead of scrambling around each brokerage to get orders in manually.

## Current Broker Support
* **Alpaca**: requires secret and public access key
* **Tradier**: requires account id and access token
* **Robinhood**: requires username, password and MFA setup token
* **StockTwits**: requires access token

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

## To Do
* Add async support ?
* Add encryption to credentials
* Fully automate by tracking FINRA and/or SEC filings
* Add more brokers (Firstrade, Webull, Schwab(if the TDA API sticks around when the companies are consolidated))
* Add per trade logging to a CSV
* maybe add a menu with entries for buy sell setup, to avoid having to rerun script after setup.
