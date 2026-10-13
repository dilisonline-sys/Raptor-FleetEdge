# Contributing to Raptor FleetEdge

Thanks for your interest in improving FleetEdge! Bug reports, strategy ideas, dashboard improvements and documentation fixes are all welcome.

## Ground rules

- **Never test against real funds.** Develop and verify changes in `testnet` or `demo` mode only.
- **Never commit secrets.** Keep API keys in `.env` (already in `.gitignore`). If you paste logs into an issue, remove keys, balances and account IDs first.
- **Keep changes focused.** One fix or feature per pull request is much easier to review.
- **Be kind.** Assume good intent and keep discussion about the code.

## Getting set up

```bash
git clone https://github.com/dilisonline-sys/Raptor-FleetEdge.git
cd Raptor-FleetEdge
cp .env.example .env          # set TRADING_MODE=demo and add demo/testnet keys
sudo docker compose up -d --build
```

The fleet dashboard is then at http://localhost:7430. To run without Docker, use Python 3.13 and `pip install -r requirements.txt`.

## Reporting bugs

Open an issue using the **Bug report** template and include:

- what you expected and what happened instead
- trading mode (`testnet` / `demo` / `live`) and how you run it (Docker or plain Python)
- relevant agent log lines (see the **Logs** section of the README — `/tmp/rfe_<name>.log` in the container), with secrets removed
- steps to reproduce, if you can

## Proposing a feature or strategy

Open an issue using the **Feature request** template before writing a large change, so we can agree on the approach first.

### Adding a strategy module

Strategies live in their own `*_module.py` file and expose a single function:

```python
def signal_engine(indicators: dict, override: str | None = None) -> str:
    """Return "BUY", "SELL" or "NONE" from the indicator dict."""
```

See `rsi_reversal_module.py` for a compact example. A new strategy also needs to be:

1. imported and listed in the strategy names in `agent.py`, and added to the signal dispatch (primary and secondary branches)
2. added to the strategy selector list in `instruction_server.py`
3. given a scoring rule in `strategy_advisor_module.py`
4. documented in the **Strategies** table in `README.md`

Please include in your pull request how the strategy behaved in `demo` mode (time period, number of trades, win rate and net result after fees) — including if it lost money. Honest results help everyone.

## Pull requests

1. Fork the repository and create a branch from `main` (e.g. `fix/stop-rounding` or `feat/supertrend-strategy`).
2. Make your change, following the existing code style (plain Python, type hints where helpful, logging via `logger.log`).
3. Run the fleet in `demo` mode and confirm the agents start, the dashboard loads and nothing new appears in the error logs.
4. Update `README.md` if behaviour, configuration or defaults change.
5. Open the pull request with a clear description of what changed and why.

## Security issues

Please don't open public issues for vulnerabilities — see [SECURITY.md](SECURITY.md).
