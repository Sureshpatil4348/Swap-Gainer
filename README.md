Dual MT5 Bridge UI (Python)

Overview

This app connects to two MetaTrader 5 terminals on Windows and lets you place simultaneous BUY/SELL market orders on both accounts with one click. Each pair of trades is tracked as a single row with combined net profit and a Close (both) action.

Features

- Connect to two local MT5 terminals via their .lnk shortcuts or .exe paths
- Simultaneous BUY/SELL across both accounts (two worker processes, one per terminal)
- Track only app-placed positions as paired threads
- Combined Net Profit auto-updates
- Close both trades of a pair instantly

Requirements

- Windows with two MT5 terminals installed and logged in
- Python 3.10+
- Packages: MetaTrader5, pywin32

Install

1. Open PowerShell in the project folder.
2. Create a virtual environment and install deps:

   Powershell:
   `py -3 -m venv .venv; .\.venv\Scripts\python -m pip install -U pip; .\.venv\Scripts\pip install -r requirements.txt`

Run

` .\.venv\Scripts\python main.py `

Usage

1. Leave the default terminal paths or provide your own (.lnk or terminal64.exe).
2. Click Connect Both. Status should turn "connected" for each.
3. Enter symbols and lot sizes for Account 1 and Account 2.
4. Click BUY (Simultaneous) or SELL (Simultaneous).
5. Manage pairs in the table; use Close on a row to close both trades.

Notes

- The app resolves .lnk shortcuts to the target terminal automatically (requires pywin32).
- Only trades created by this app are shown in the table.
- Profit values refresh periodically; closed pairs are removed from the table.

Persistence
-----------

- The UI persists its session data (paired trades, trade counter, and the last used terminal
  paths) to ``%USERPROFILE%\.swap_gainer\state.json`` on Windows (or ``~/.swap_gainer/state.json``
  on other platforms).
- State is saved after every trade create/close event and again during shutdown so that the app
  can restore open positions on the next launch.
- When the application starts it automatically reloads the saved file, reconnects to the recorded
  terminals, validates that the saved tickets are still open, and repopulates the table for any
  trades that remain active. Closed or invalid entries are discarded.
- If the JSON file becomes corrupted or you need to reset the saved state, exit the application,
  delete ``state.json`` (or move it aside), and restart the UI. The app will report any load/save
  errors in a pop-up dialog so operators are aware of persistence issues.


