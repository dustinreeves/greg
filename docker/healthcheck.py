"""Container health check: the web UI must answer, with a login request."""
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8787/", timeout=5)
except urllib.error.HTTPError as e:
    # 401 = up and asking for a password, which is what we want
    sys.exit(0 if e.code == 401 else 1)
except Exception:
    sys.exit(1)
sys.exit(1)  # answered 200 without a login: something is wrong
