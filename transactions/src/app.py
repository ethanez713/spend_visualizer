"""Tiny Flask backend for the Plaid Link flow.

Launched via the project-root shim:  ./venv/bin/python app.py --user <name>
Then open http://127.0.0.1:5000/ in a browser, once per bank. Every Item linked
in the session is owned by --user; restart with the other name for their banks.
"""
import argparse

from flask import Flask, jsonify, request, send_from_directory
from plaid.model.country_code import CountryCode
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.products import Products

from .plaid_client import BASE_DIR, add_token, get_client, load_tokens, validate_owner

app = Flask(__name__)
client = get_client()


@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "link.html")


@app.route("/linked")
def linked():
    """Running list of already-linked institutions."""
    # require_owner=False: this must keep working on a pre-migration tokens file.
    return jsonify([t["institution"] for t in load_tokens(require_owner=False)])


@app.route("/create_link_token", methods=["POST"])
def create_link_token():
    req = LinkTokenCreateRequest(
        user=LinkTokenCreateRequestUser(client_user_id=app.config["OWNER"]),
        client_name="Transactions Exporter",
        products=[Products("transactions")],
        country_codes=[CountryCode("US")],
        language="en",
        # Request the maximum history (24 months). Default is 90 days, and this
        # value is fixed at Item creation — it cannot be changed later.
        transactions=LinkTokenTransactions(days_requested=730),
    )
    resp = client.link_token_create(req)
    return jsonify({"link_token": resp["link_token"]})


@app.route("/exchange", methods=["POST"])
def exchange():
    body = request.get_json(silent=True) or {}
    public_token = body.get("public_token")
    if not public_token:
        return jsonify({"error": "missing public_token"}), 400

    # public_token -> access_token + item_id (persist immediately)
    try:
        exch = client.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        )
    except Exception as e:  # bad/expired token, Plaid outage — return JSON, not an HTML 500
        app.logger.warning("Token exchange failed: %s", e)
        return jsonify({"error": "token exchange failed"}), 502
    access_token = exch["access_token"]
    item_id = exch["item_id"]

    # Resolve a human-readable institution name.
    institution = "Unknown institution"
    try:
        item = client.item_get(ItemGetRequest(access_token=access_token))
        inst_id = item["item"]["institution_id"]
        if inst_id:
            inst = client.institutions_get_by_id(
                InstitutionsGetByIdRequest(
                    institution_id=inst_id, country_codes=[CountryCode("US")]
                )
            )
            institution = inst["institution"]["name"]
    except Exception as e:  # name is cosmetic — never lose the token over it
        app.logger.warning("Could not resolve institution name: %s", e)

    add_token(access_token, item_id, institution, owner=app.config["OWNER"])
    return jsonify({"institution": institution, "item_id": item_id})


def main(argv=None):
    p = argparse.ArgumentParser(description="Plaid Link backend — link banks for one user.")
    p.add_argument("--user", required=True, metavar="NAME",
                   help="who the linked banks belong to (stored on each token entry)")
    args = p.parse_args(argv)
    owner = validate_owner(args.user)

    known = {t.get("owner") for t in load_tokens(require_owner=False)} - {None}
    new_note = "" if owner in known or not known else "  (NEW user — check for typos!)"
    print(f"Linking banks for user: {owner}{new_note}")

    app.config["OWNER"] = owner
    # Local tool; bind to localhost only.
    app.run(host="127.0.0.1", port=5000, debug=False)


if __name__ == "__main__":
    main()
