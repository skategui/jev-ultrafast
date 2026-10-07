"""Sign in through a local app's API and hand the session to Jev's Chrome.

    jev-session --base http://localhost:3101 --email doctor@example.test --password-env DOC_PW \
        [--mfa-code 246810]
    jev-session --base http://localhost:3101 --clear

POSTs {email, password} to --login-path; if the answer carries a challengeId, POSTs
{challengeId, code} to --mfa-path. The cookies the app sets (httpOnly included) are written
into the Chrome Jev drives (:9333, kept running), so `jev --url <base>/...` starts signed in.

Local hosts only (localhost, 127.0.0.1, *.localhost, *.test): a real account's session must
never land in a debugging browser by accident. Prints cookie names, never values.
Exit codes: 0 = signed in / cleared, 1 = sign-in or MFA refused by the app, 2 = not a local host.
"""

import argparse
import http.cookiejar
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from urllib.parse import urlsplit

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from navigate import ensure_chrome  # noqa: E402

LOCAL_SUFFIXES = (".localhost", ".test")


def is_local(base):
    host = urlsplit(base).hostname or ""
    return host in {"localhost", "127.0.0.1"} or host.endswith(LOCAL_SUFFIXES)


def post(opener, url, body):
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def sign_in(base, args):
    password = os.environ.get(args.password_env or "")
    if not password:
        raise SystemExit(f"--password-env {args.password_env}: variable is unset or empty")
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    status, answer = post(opener, base + args.login_path, {"email": args.email, "password": password})
    if status != 200:
        raise SystemExit(f"sign-in refused: HTTP {status} {answer.get('code', '')}")
    if answer.get("challengeId"):
        if not args.mfa_code:
            raise SystemExit("the app asks for an MFA code: pass --mfa-code")
        status, answer = post(
            opener, base + args.mfa_path, {"challengeId": answer["challengeId"], "code": args.mfa_code}
        )
        if status != 200:
            raise SystemExit(f"MFA refused: HTTP {status} {answer.get('code', '')}")
    if not len(jar):
        raise SystemExit("sign-in answered 200 but set no cookie")
    return jar


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", required=True, help="app origin, e.g. http://localhost:3101")
    parser.add_argument("--email")
    parser.add_argument("--password-env", metavar="VAR", help="env var holding the password")
    parser.add_argument("--mfa-code")
    parser.add_argument("--login-path", default="/api/auth/login")
    parser.add_argument("--mfa-path", default="/api/auth/mfa/verify")
    parser.add_argument("--clear", action="store_true", help="delete this origin's session cookies instead")
    parser.add_argument("--chrome-port", type=int, default=9333)
    parser.add_argument("--chrome-profile", default="/tmp/jev-chrome-profile")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    if not is_local(base):
        print(f"refused: {base} is not a local host", file=sys.stderr)
        return 2
    if not args.clear and not args.email:
        parser.error("--email is required unless --clear")

    jar = None if args.clear else sign_in(base, args)
    # keep=True: the Chrome holding the session must outlive this script for the next `jev` run.
    ensure_chrome(args.chrome_port, args.chrome_profile, None, True)
    from browser_harness.admin import ensure_daemon
    from browser_harness.helpers import cdp

    ensure_daemon()
    if args.clear:
        names = [c["name"] for c in cdp("Network.getCookies", urls=[base + "/"])["cookies"]]
        for name in names:
            cdp("Network.deleteCookies", name=name, url=base + "/")
        print(f"cleared {len(names)} cookies for {base}: {', '.join(names)}")
        return 0
    for c in jar:
        cdp(
            "Network.setCookie",
            name=c.name,
            value=c.value,
            url=base + "/",
            path=c.path or "/",
            httpOnly=c.has_nonstandard_attr("HttpOnly"),
            secure=bool(c.secure),
            **({"expires": c.expires} if c.expires else {}),
        )
    print(f"signed in {args.email} on {base}: injected {', '.join(c.name for c in jar)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
