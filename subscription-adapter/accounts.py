"""Personal Sign in with ChatGPT, using the documented public OAuth flow."""
import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import struct
import threading
import time
import uuid
from urllib.parse import urlencode, urlsplit

import jwt
import requests

from protocol import ProtocolError

AUTHORITY = "https://auth.openai.com"
TOKEN_URL = AUTHORITY + "/api/accounts/oauth/token"
API_RESOURCE = "https://api.openai.com/v1"
DIRECT_SCOPE = "chatgpt.tokens.use.direct"
SCOPES = "openid profile email offline_access resource.invoke " + DIRECT_SCOPE


class ProtectedStore:
    """Windows DPAPI belongs to the local Windows account; no shared auth cache."""
    def __init__(self, directory):
        if os.name != "nt":
            raise RuntimeError("This first adapter build supports Windows protected storage only")
        import win32api
        import win32con
        import win32security
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            token.Close()
        acl = win32security.ACL()
        acl.AddAccessAllowedAceEx(win32security.ACL_REVISION,
            win32con.OBJECT_INHERIT_ACE | win32con.CONTAINER_INHERIT_ACE, win32con.GENERIC_ALL, sid)
        win32security.SetNamedSecurityInfo(str(self.directory), win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None, None, acl, None)
        self.lock = threading.RLock()

    def read(self, name):
        import win32crypt
        path = self.directory / (name + ".dpapi")
        with self.lock:
            if not path.exists():
                return None
            raw = win32crypt.CryptUnprotectData(path.read_bytes(), None, None, None, 0)[1]
            return json.loads(raw)

    def write(self, name, value):
        import win32crypt
        with self.lock:
            path = self.directory / (name + ".dpapi")
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            protected = win32crypt.CryptProtectData(raw, "Iikona personal connection", None, None, None, 0)
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_bytes(protected)
            os.replace(temporary, path)

    def delete(self, name):
        with self.lock:
            (self.directory / (name + ".dpapi")).unlink(missing_ok=True)

    def append_raw(self, name, raw):
        import win32crypt
        protected = win32crypt.CryptProtectData(raw, "Iikona provider event", None, None, None, 0)
        with self.lock, (self.directory / (name + ".events.dpapi")).open("ab") as stream:
            stream.write(struct.pack("<I", len(protected)))
            stream.write(protected)
            stream.flush()

    def read_raw(self, name):
        import win32crypt
        with self.lock, (self.directory / (name + ".events.dpapi")).open("rb") as stream:
            result = []
            while header := stream.read(4):
                if len(header) != 4:
                    raise ValueError("Incomplete event header")
                length = struct.unpack("<I", header)[0]
                block = stream.read(length)
                if len(block) != length:
                    raise ValueError("Incomplete encrypted event")
                result.append(win32crypt.CryptUnprotectData(block, None, None, None, 0)[1])
            return result


class ChatGPTAccount:
    def __init__(self, store, session=None):
        self.store = store
        self.http = session or requests.Session()
        # Session adapters have no retry policy. Never retry a model request.
        self.lock = threading.RLock()
        self.pending = None
        host = store.read("host")
        if not host:
            host = {"ext_agent_host_id": "urn:uuid:" + str(uuid.uuid4()),
                    "pairing_token": secrets.token_urlsafe(32)}
            store.write("host", host)
        self.host = host

    def status(self):
        account = self.store.read("chatgpt")
        return {"provider": "chatgpt", "connected": bool(account),
                "plan_permission": bool(account and DIRECT_SCOPE in account.get("scopes", [])),
                "login_pending": self.pending is not None,
                "billing": "subscription", "cost": None}

    def start_login(self, callback):
        with self.lock:
            current = self.store.read("chatgpt")
            verifier = secrets.token_urlsafe(48)
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            attempt = {"state": secrets.token_urlsafe(32), "nonce": secrets.token_urlsafe(32),
                       "verifier": verifier, "callback": callback, "started": time.time(),
                       "client_id": current["client_id"] if current else "dynamic_agent_client",
                       "subject": current["subject"] if current else None}
            query = {"client_id": attempt["client_id"], "ext_agent_host_id": self.host["ext_agent_host_id"],
                "response_type": "code", "redirect_uri": callback, "scope": SCOPES,
                "resource": API_RESOURCE, "state": attempt["state"], "nonce": attempt["nonce"],
                "code_challenge_method": "S256", "code_challenge": challenge}
            if current:
                query["id_token_hint"] = current["id_token"]
            else:
                query["agent_name_hint"] = "Iikona"
            self.pending = attempt
            return {"authorization_url": AUTHORITY + "/api/accounts/authorize?" + urlencode(query),
                    "expires_in": 600}

    def finish_login(self, query):
        with self.lock:
            attempt = self.pending
            if not attempt or time.time() - attempt["started"] > 600:
                raise ProtocolError("login_expired", "Start a new sign-in attempt")
            if not secrets.compare_digest(query.get("state", ""), attempt["state"]):
                raise ProtocolError("invalid_state", "Sign-in state does not match")
            # The code and verifier are single-use, including failed exchanges.
            self.pending = None
            if "error" in query:
                raise ProtocolError("consent_declined", "ChatGPT sign-in was not completed")
            issued = query.get("client_id") or attempt["client_id"]
            if issued == "dynamic_agent_client" or not isinstance(issued, str) or not issued:
                raise ProtocolError("registration_incomplete", "An issued client ID is required")
            if attempt["client_id"] != "dynamic_agent_client" and issued != attempt["client_id"]:
                raise ProtocolError("account_mismatch", "The selected registration changed")
            code = query.get("code")
            if not code:
                raise ProtocolError("missing_code", "No authorization code was returned")
            tokens = self._token_request({"grant_type": "authorization_code", "client_id": issued,
                "code": code, "code_verifier": attempt["verifier"], "redirect_uri": attempt["callback"],
                "resource": API_RESOURCE})
            claims = self._validate_identity(tokens.get("id_token", ""), issued, attempt["nonce"])
            if attempt["subject"] and claims["sub"] != attempt["subject"]:
                raise ProtocolError("account_mismatch", "A different ChatGPT account signed in")
            record = self._token_record(tokens)
            record.update({"client_id": issued, "subject": claims["sub"], "issuer": claims["iss"],
                           "ext_agent_host_id": self.host["ext_agent_host_id"]})
            self.store.write("chatgpt", record)
            return self.status()

    def _validate_identity(self, token, client_id, nonce):
        try:
            discovery = self.http.get(AUTHORITY + "/.well-known/openid-configuration", timeout=30)
            discovery.raise_for_status()
            metadata = discovery.json()
            if metadata.get("issuer") != AUTHORITY:
                raise ValueError("Unexpected identity issuer")
            jwks_uri = metadata["jwks_uri"]
            parsed = urlsplit(jwks_uri)
            if parsed.scheme != "https" or parsed.netloc != "auth.openai.com":
                raise ValueError("Unexpected signing-key host")
            response = self.http.get(jwks_uri, timeout=30)
            response.raise_for_status()
            key_id = jwt.get_unverified_header(token)["kid"]
            keys = [key for key in response.json()["keys"] if key.get("kid") == key_id]
            if len(keys) != 1:
                raise ValueError("Missing signing key")
            key = jwt.PyJWK.from_dict(keys[0])
            claims = jwt.decode(token, key.key, algorithms=["RS256"], audience=client_id,
                issuer=AUTHORITY, options={"require": ["sub", "exp", "iat", "nonce"]})
            if not secrets.compare_digest(claims["nonce"], nonce):
                raise ValueError("Nonce mismatch")
            return claims
        except (ValueError, KeyError, jwt.PyJWTError, requests.RequestException) as error:
            raise ProtocolError("invalid_identity", "ChatGPT identity could not be verified") from error

    def _token_request(self, body):
        try:
            response = self.http.post(TOKEN_URL, data=body, timeout=45, allow_redirects=False)
            if response.status_code != 200:
                raise ProtocolError("login_required", "ChatGPT authorization must be renewed")
            return response.json()
        except (requests.RequestException, ValueError) as error:
            raise ProtocolError("authorization_unavailable", "Authorization endpoint is unavailable") from error

    @staticmethod
    def _token_record(tokens, previous=None):
        record = dict(previous or {})
        if not isinstance(tokens.get("access_token"), str) or not tokens["access_token"]:
            raise ProtocolError("invalid_tokens", "Authorization returned no access token")
        scopes = tokens.get("scope", "").split() if "scope" in tokens else record.get("scopes", [])
        if DIRECT_SCOPE not in scopes:
            raise ProtocolError("plan_permission_required", "ChatGPT plan usage was not authorized")
        expires = tokens.get("expires_in")
        if not isinstance(expires, (int, float)) or expires <= 0:
            raise ProtocolError("invalid_tokens", "Authorization returned no token expiry")
        record.update({key: tokens[key] for key in ("access_token", "refresh_token", "id_token") if key in tokens})
        record.update(scopes=scopes, expires_at=time.time() + expires)
        return record

    def access_token(self):
        with self.lock:
            record = self.store.read("chatgpt")
            if not record:
                raise ProtocolError("login_required", "Sign in with ChatGPT first")
            if record["expires_at"] <= time.time() + 60:
                if not record.get("refresh_token"):
                    raise ProtocolError("login_required", "Sign in with ChatGPT again")
                tokens = self._token_request({"grant_type": "refresh_token", "client_id": record["client_id"],
                    "refresh_token": record["refresh_token"], "resource": API_RESOURCE})
                record = self._token_record(tokens, record)
                self.store.write("chatgpt", record)
            return record["access_token"]

    def logout(self):
        with self.lock:
            self.pending = None
            self.store.delete("chatgpt")
