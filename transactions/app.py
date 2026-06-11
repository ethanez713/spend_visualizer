"""Entry point — Plaid Link Flask server. Implementation in src/app.py.

Run:  ./venv/bin/python app.py
Then open http://127.0.0.1:5000/ in a browser, once per bank.
"""
from src.app import main

if __name__ == "__main__":
    main()
