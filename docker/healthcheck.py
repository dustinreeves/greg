"""Container health check: the web UI must be up and answering."""
import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8787/", timeout=5)
except urllib.error.HTTPError as e:
    # 401: asking for a login. 400: refused our Host header (allowed_hosts is
    # set, and we are not asking as that host). Either way greg is up.
    # 403/429: refused or locked out, still up. 5xx or no answer: not healthy.
    sys.exit(0 if e.code in (400, 401, 403, 429) else 1)
except Exception:
    sys.exit(1)
sys.exit(1)  # answered 200 without a login: something is wrong
