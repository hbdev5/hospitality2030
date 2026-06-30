#!/usr/bin/env python3
"""
Verify Plivo SMS is configured and working.

Read-only by default: checks the PLIVO_* credentials are present and that they
authenticate against Plivo (account fetch — sends NO message). Add --to and
--send to fire a single transactional test SMS.

  python3 scripts/verify_sms.py                          # config + auth check only
  python3 scripts/verify_sms.py --to +16464408480 --send # live send test

Run on the VM (where the venv + .env live):
  ssh -i ~/work/ssh-keys/HB-New_key.pem azureuser@20.127.222.82
  cd /home/azureuser/work/recsys && python3 scripts/verify_sms.py
"""
import argparse, os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.config import get_settings


def main():
    ap = argparse.ArgumentParser(description="Verify Plivo SMS config / auth / send.")
    ap.add_argument("--to", help="destination number, E.164 e.g. +16464408480")
    ap.add_argument("--send", action="store_true", help="actually send a test SMS to --to")
    args = ap.parse_args()

    s = get_settings()

    missing = [k for k in ("plivo_auth_id", "plivo_auth_token", "plivo_number")
               if not getattr(s, k)]
    if missing:
        print(f"FAIL  Missing Plivo settings: {', '.join(missing)}  (set them in .env)")
        return 1
    print(f"OK    Plivo creds present  (auth_id {s.plivo_auth_id[:6]}..., from {s.plivo_number})")

    try:
        import plivo
    except ImportError:
        print("FAIL  plivo SDK not installed here (pip install plivo). Run this on the VM.")
        return 1

    client = plivo.RestClient(s.plivo_auth_id, s.plivo_auth_token)

    # Read-only auth probe — confirms the credentials work without sending anything.
    try:
        acct = client.account.get()
        print(f"OK    Authenticated to Plivo  (account: {getattr(acct, 'name', '?')}, "
              f"cash_credits: {getattr(acct, 'cash_credits', '?')})")
    except Exception as e:
        print(f"FAIL  Plivo auth failed: {e}")
        return 1

    if args.send:
        if not args.to:
            print("FAIL  --send requires --to +1XXXXXXXXXX")
            return 1
        try:
            resp = client.messages.create(
                src=s.plivo_number, dst=args.to,
                text="Oak & Ivy: SMS verification test. Reply STOP to opt out.")
            print(f"OK    Test SMS sent to {args.to}  (uuid: {getattr(resp, 'message_uuid', resp)})")
        except Exception as e:
            print(f"FAIL  Send failed: {e}")
            return 1
    else:
        print("INFO  Auth verified without sending. Re-run with --to <number> --send to test a live message.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
