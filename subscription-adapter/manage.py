"""Manage the local personal connection without exposing provider credentials."""
import argparse
import json
import urllib.error
import urllib.request

from accounts import ProtectedStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True, help='The same private folder passed to server.py')
    parser.add_argument('--port', type=int, default=18743)
    parser.add_argument('action', choices=['status', 'login', 'logout', 'models', 'pairing', 'receipts'])
    parser.add_argument('--older-than-days', type=int, default=90)
    parser.add_argument('--apply-plan', help='Apply exactly the reviewed receipt archive plan SHA-256')
    args = parser.parse_args()
    store = ProtectedStore(args.data)
    host = store.read('host')
    if not host:
        parser.error('Start the personal adapter first')
    if args.action == 'pairing':
        print('Personal pairing code. Enter it in your own 1C connection; do not share it.')
        print(host['pairing_token'])
        return
    path = '/v1/models' if args.action == 'models' else '/connection/' + args.action
    body = b'{}' if args.action in ['login', 'logout'] else None
    if args.action == 'receipts':
        path = '/maintenance/receipts'
        parameters = {'older_than_days': args.older_than_days}
        if args.apply_plan is not None:
            parameters['apply_plan_sha256'] = args.apply_plan
        body = json.dumps(parameters).encode('utf-8')
    request = urllib.request.Request(f'http://127.0.0.1:{args.port}' + path,
        data=body,
        headers={'Authorization': 'Bearer ' + host['pairing_token'], 'Content-Type': 'application/json'})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=60) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(1) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
