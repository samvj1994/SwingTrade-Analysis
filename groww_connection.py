"""Secure Groww API client creation from process environment variables."""

from __future__ import annotations

import os

from dotenv import load_dotenv


load_dotenv()


def get_groww_client():
    """Create an authenticated Groww client from an access token or key pair."""
    from growwapi import GrowwAPI

    access_token = os.environ.get("GROWW_ACCESS_TOKEN")
    if access_token:
        if len(access_token) < 40:
            raise RuntimeError(
                "GROWW_ACCESS_TOKEN looks too short to be a Groww access token. "
                "If you copied the API key from Groww, rename it to GROWW_API_KEY "
                "and also add GROWW_API_SECRET; do not put the API key in the "
                "access-token field."
            )
        return GrowwAPI(access_token)

    api_key = os.environ.get("GROWW_API_KEY")
    api_secret = os.environ.get("GROWW_API_SECRET")
    if not api_key or not api_secret:
        raise RuntimeError(
            "Set GROWW_ACCESS_TOKEN or both GROWW_API_KEY and GROWW_API_SECRET "
            "in the .env file before starting the dashboard."
        )

    access_token = GrowwAPI.get_access_token(api_key=api_key, secret=api_secret)
    return GrowwAPI(access_token)
