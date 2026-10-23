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
    parser.add_argument('action', choices=['status', 'login', 'logout', 'models', 'pairing'])
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
    request = urllib.request.Request(f'http://127.0.0.1:{args.port}' + path,
        data=b'{}' if args.action in ['login', 'logout'] else None,
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
