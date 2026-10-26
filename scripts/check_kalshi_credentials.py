import os
import sys
import time
import base64
import urllib.request
import urllib.error
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

key_id = os.environ.get("KALSHI_API_KEY_ID", "").strip()
private_pem = os.environ.get("KALSHI_PRIVATE_KEY", "")
if not key_id or not private_pem:
    print("FAIL: one or both Kalshi GitHub Secrets are missing or empty.")
    sys.exit(2)

try:
    private_key = serialization.load_pem_private_key(private_pem.encode(), password=None)
except Exception:
    print("FAIL: KALSHI_PRIVATE_KEY is not a valid unencrypted PEM private key.")
    sys.exit(2)

timestamp = str(int(time.time() * 1000))
path = "/trade-api/v2/portfolio/balance"
message = (timestamp + "GET" + path).encode()
signature = private_key.sign(
    message,
    padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
    hashes.SHA256(),
)
url = "https://external-api.kalshi.com" + path
request = urllib.request.Request(url, method="GET", headers={
    "KALSHI-ACCESS-KEY": key_id,
    "KALSHI-ACCESS-TIMESTAMP": timestamp,
    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    "Accept": "application/json",
})
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        if response.status == 200:
            print("PASS: Kalshi accepted the API credentials; read-only balance request succeeded.")
            sys.exit(0)
        print("FAIL: Kalshi returned HTTP " + str(response.status) + ".")
        sys.exit(1)
except urllib.error.HTTPError as exc:
    if exc.code in (401, 403):
        print("FAIL: Kalshi rejected authentication/authorization (HTTP " + str(exc.code) + "). Check the API Key ID/private-key pair and key permissions.")
    else:
        print("FAIL: Kalshi returned HTTP " + str(exc.code) + ".")
    sys.exit(1)
except Exception as exc:
    print("FAIL: request could not complete (" + type(exc).__name__ + "). Check network/API availability.")
    sys.exit(1)
