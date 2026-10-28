"""UniFi OS login + Protect WebSocket token (used by protect_token.py)."""
import requests
import urllib3

# Disable insecure request warnings for local self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

class ProtectAuth:
    def __init__(self, host, username, password):
        self.host = host
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.session.verify = False
        self.token = None

    def login(self):
        """Log in to UniFi OS and retrieve the session token."""
        url = f"https://{self.host}/api/auth/login"
        payload = {
            "username": self.username,
            "password": self.password
        }
        
        print(f"Attempting login to {url}...")
        try:
            response = self.session.post(url, json=payload, timeout=10)
            
            if response.status_code == 200:
                print("Login successful!")
                return True
            else:
                print(f"Login failed with status code: {response.status_code}")
                return False
        except Exception as e:
            print(f"An error occurred during login: {e}")
            return False

    def get_ws_token(self):
        """Retrieve a specialized token for WebSocket authentication using CSRF."""
        # 1. Get CSRF token first
        csrf_token = None
        try:
            url_bootstrap = f"https://{self.host}/proxy/protect/api/bootstrap"
            resp = self.session.get(url_bootstrap, timeout=10)
            csrf_token = resp.headers.get('X-CSRF-Token')
        except:
            pass

        # 2. Use CSRF to get the specialized token
        try:
            url = f"https://{self.host}/proxy/protect/api/auth/token"
            headers = {"X-CSRF-Token": csrf_token} if csrf_token else {}
            response = self.session.post(url, headers=headers, timeout=10)
            if response.status_code == 200:
                token = response.json().get('token')
                if token:
                    print("Found specialized Protect token with CSRF")
                    return token
        except:
            pass
            
        # 3. Fallback to session cookie
        return self.session.cookies.get('TOKEN')
