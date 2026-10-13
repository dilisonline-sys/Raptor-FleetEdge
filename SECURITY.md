# Security Policy

FleetEdge handles exchange API keys and can place real orders, so security reports are taken seriously.

## Reporting a vulnerability

**Please do not open a public issue.** Instead, report it privately through GitHub:

1. Go to the repository's **Security** tab.
2. Click **Report a vulnerability** and describe the issue, how to reproduce it, and its potential impact.

You should receive a response within a few days. Please give us reasonable time to release a fix before disclosing the issue publicly.

## Keeping your deployment safe

- Give your Binance API key **Trade + Read permissions only — never enable withdrawals**.
- Restrict the API key to your server's IP address.
- Never commit `.env` or share logs that contain keys, balances or account IDs.
- The dashboards and instruction endpoints are designed for local use. Do not expose ports 7430–7438 to the public internet; if you need remote access, put them behind a VPN or an authenticated reverse proxy.
- Set your own values for `DIPU_AUTHORIZED_AGENT_TOKENS`. Internal manager-to-agent calls use a fixed token, so network isolation is what protects the instruction endpoints.
