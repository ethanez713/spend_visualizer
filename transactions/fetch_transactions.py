"""Entry point — fetch all Plaid transactions into transactions.csv.
Implementation in src/fetch_transactions.py.

Run:  ./venv/bin/python fetch_transactions.py
Safe to re-run / schedule — only new or changed transactions are pulled.
"""
from src.fetch_transactions import main

if __name__ == "__main__":
    main()
