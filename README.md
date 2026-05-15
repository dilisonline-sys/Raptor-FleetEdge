dipu — Crypto Trading Agent
Persona

20-year veteran. High-risk / high-reward. Sizes aggressively on pristine setups, sits flat when the market is volatile or ambiguous. Capital preservation is the floor, not the goal.
Files
File 	Role
agent.py 	Main loop — orchestrates all 8 modules
market_data.py 	Module 1: feeds, order book, indicators (RSI, EMA, MACD, ATR, BB, VWAP)
sizing.py 	Module 2: position sizing with volatility adjustment
order_manager.py 	Module 3/4: order submission, retries, fill tracking
exit_manager.py 	Module 5: hard stops, break-even, trailing stops, TP1/2/3 ladder
risk_engine.py 	Module 6/7: drawdown tracking, kill switch, alerts
regime.py 	Module 8: TRENDING / RANGING / VOLATILE classification
instruction_server.py 	HTTP server — accepts signals from authorized external agents
config.py 	All parameters in one place
dipu's aggressive parameters vs spec defaults
Parameter 	Spec default 	dipu
Risk per trade 	1% 	2%
Max single trade 	5% 	8%
Max total exposure 	20% 	30%
Max leverage 	3× 	5×
Daily halt threshold 	5% 	7%
TP1 target 	1.5R 	2.0R
TP3 target 	4.0R 	6.0R
Multi-agent instruction interface

    Runs an HTTP server on port 7432
    External bots/agents POST to /instruction with X-Agent-Token header
    Valid actions: BUY, SELL, CLOSE_ALL, HALT, STATUS
    The Binance login account is intentionally excluded — it is execution-only, not an instruction source
    Tokens configured via DIPU_AUTHORIZED_AGENT_TOKENS env var

To run

cd dipu_agent
cp .env.example .env
# fill in BINANCE_API_KEY, BINANCE_API_SECRET, DIPU_AUTHORIZED_AGENT_TOKENS
pip install -r requirements.txt
python agent.py   # starts on testnet by default

Set BINANCE_TESTNET=false in .env when ready to go live. The spec's go-live checklist (Section 5) applies — minimum 2 weeks paper trading first.
