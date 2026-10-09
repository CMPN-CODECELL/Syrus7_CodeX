# How to run TradeShield with your real 021 account

## 0. What you need installed
* **Python 3.10 or newer** (check: `python --version`; on Mac/Linux `python3 --version`)
* **Node.js 18 or newer** (check: `node --version`)
* Your 021 account: UCC (looks like `HACK1234`) and the password you chose at sign-up

> There is no separate "API key" in the 021 sandbox: the API login is your **UCC + password**.
> The platform logs in for you and handles the token. Use the UCC, not your email.

## 1. Unzip and open a terminal in the folder
Unzip `tradeshield_021.zip`. Open a terminal (Windows: PowerShell or Command Prompt) inside the `tradeshield` folder.

## 2. Put your credentials in `.env`
* Windows: `copy .env.example .env`      Mac/Linux: `cp .env.example .env`
* Open `.env` in any text editor and set
  ```
  O21_USERNAME=HACK1234          <- your UCC
  O21_PASSWORD=the-password-you-chose
  TS_BROKER=021
  ```
* Save. Never share or upload this file (it is already in `.gitignore`).

## 3. Backend: install, test, check the connection
```bash
cd backend
python -m venv .venv
.venv\Scripts\activate            # Mac/Linux:  source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests      # expect: Ran 72 tests ... OK
python check_021.py                       # expect: login OK, 3 instrument tokens, prices (if market is open)
```
`check_021.py` places **no** orders. If it prints `login OK` you are connected. Fix any error it shows before going on (see Troubleshooting).

## 4. Start the backend (keep this terminal open)
```bash
uvicorn tradeshield.app:app --port 8000
```
Run exactly **one** copy, without `--reload`. 021 allows only one live token per account; a second copy (or logging in from Postman) logs the first one out.
You should see `021 broker ready. Instrument tokens: ...`.

## 5. Start the dashboard (second terminal)
```bash
cd frontend
npm install
npm run dev
```
Open **http://localhost:5173** . The UI is exactly the one from your project; it talks to the backend on port 8000.

(Shortcut on Windows: double-click `run_backend.bat`, then `run_frontend.bat`. Mac/Linux: `./run_backend.sh`, `./run_frontend.sh`.)

## 6. Use it
1. **Register** a platform account (this is TradeShield's own login, separate from 021).
2. **Subscribe** to strategies: *Time-Based Entry/Exit* (RELIANCE), *1% Breakout from Open* (INFY), *Trend Breakout* (TCS), plus others.
3. Watch live prices, candles, P&L, orders and the Activity log. Open *Settings* on a strategy to change risk limits.
4. **Kill switch** button: cancels open orders and closes positions (target: under 10 s).
5. The Time-Based strategy only acts at 09:15 IST (inside 09:15-09:20) and squares off at 15:15 IST. To demo it at another time, open
   http://localhost:8000/docs, call `PUT /api/subscriptions/{id}/params` (paste your platform token into the `authorization` field as `Bearer <token>`)
   with `{"params": {"entry_time": "14:30", "exit_time": "14:40"}}` (times in IST).

## 7. Offline demo without 021 (any time)
Set `TS_BROKER=mock` in `.env`, restart the backend. A fake exchange runs 10x faster and the *Demo and test tools* panel can inject network errors, lost acknowledgements and crashes.

## 8. Stop
Ctrl+C in both terminals. Your data stays in `backend/tradeshield.db` and `backend/o21_order_map.json` (delete both for a clean start; do this only when 021 shows no open orders/positions of yours).

## Troubleshooting
| Problem | Fix |
|---|---|
| `021 login refused (HTTP 401)` | Username must be the UCC (HACK....), not the email. Check the password. |
| Worked, then everything shows errors | Another script/Postman logged in and revoked the token. Close it and restart the backend. |
| `Could not find NSE tokens for [...]` | Set `O21_TOKENS=TCS=11536,INFY=1594,RELIANCE=2885` in `.env` (check tokens with the instruments file) or change `TS_SYMBOLS`. |
| `ModuleNotFoundError` | The virtual environment is not active, or `pip install -r requirements.txt` was skipped. |
| No prices / flat candles | Market closed (only the last snapshot arrives), or a firewall blocks `wss://devapi.021.trade`. |
| Port 8000 / 5173 in use | Close the old process, or start with `--port 8001` and change the proxy in `frontend/vite.config.js`. |
| `npm install` fails | Update Node to 18+; delete `frontend/node_modules` and retry. |
| Dashboard says "Not authenticated" | Log out and register/login again (platform accounts are stored in `tradeshield.db`). |
