"""Cached Protect WebSocket token for the SIP bridge."""
import asyncio
import os
import time

from dotenv import load_dotenv

from protect_discovery import ProtectAuth

load_dotenv(interpolate=False)  # passwords may contain "$"

UNIFI_IP = os.getenv("UNIFI_IP")
USERNAME = os.getenv("UNIFI_USERNAME")
PASSWORD = os.getenv("UNIFI_PASSWORD")

auth = ProtectAuth(UNIFI_IP, USERNAME, PASSWORD)
cached_token = None
token_timestamp = 0
_token_lock = asyncio.Lock()  # one refresh at a time


async def get_valid_token():
    global cached_token, token_timestamp
    print("Server: get_valid_token requested")
    async with _token_lock:
        now = time.time()
        # Refresh if token is older than 30 minutes
        if not cached_token or (now - token_timestamp) > 1800:
            print(f"Server: Refreshing UniFi Auth Token (Age: {now - token_timestamp:.1f}s)...")
            try:
                if auth.login():
                    print("Server: Login successful, fetching WS token...")
                    token = auth.get_ws_token()
                    if token:
                        cached_token = token
                        token_timestamp = now
                        print("Server: WS Token update successful!")
                    else:
                        print("Server: Failed to get WS token from NVR")
                else:
                    print("Server: Login FAILED (Check credentials/IP)")
            except Exception as e:
                print(f"Server: Auth Error: {e}")
        else:
            print("Server: Using cached token")
        return cached_token
