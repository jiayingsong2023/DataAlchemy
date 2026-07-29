from datetime import datetime, timedelta
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext

from config import ACCESS_TOKEN_EXPIRE_MINUTES
from config import AUTH_ALGORITHM as ALGORITHM
from config import AUTH_SECRET_KEY as SECRET_KEY
from config import DEFAULT_TENANT_ID

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/auth/login")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def decode_identity(token: str):
    """Decode a token into the smallest identity needed for authorization."""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            return None
        return {
            "username": username,
            "tenant_id": payload.get("tenant_id", DEFAULT_TENANT_ID),
            "role": payload.get("role", "user"),
        }
    except JWTError:
        return None


async def get_current_identity(token: str = Depends(oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    identity = decode_identity(token)
    if identity is None:
        raise credentials_exception
    return identity


async def get_current_user(identity: dict = Depends(get_current_identity)):
    return identity["username"]

def decode_token(token: str):
    """Utility to decode token without FastAPI dependency (for WebSockets)."""
    identity = decode_identity(token)
    return identity["username"] if identity else None
