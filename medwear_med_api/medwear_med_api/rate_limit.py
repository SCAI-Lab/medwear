from jose import jwt
from slowapi import Limiter


def get_token_identity(request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            token = auth.split(" ")[1]
            payload = jwt.get_unverified_claims(token)
            return payload.get("preferred_username", "anonymous")
        except Exception:
            pass
    return request.client.host


limiter = Limiter(key_func=get_token_identity, default_limits=["100/minute"])
