import secrets
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from app.core.config import get_settings

security_scheme = HTTPBearer(auto_error=False)

async def require_bearer_token(
    credentials: HTTPAuthorizationCredentials | None = Depends(security_scheme),
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    settings = get_settings()
    expected_token = settings.auth_token
    
    # Constant-time comparison to prevent timing attacks
    if not secrets.compare_digest(credentials.credentials, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    
    return credentials.credentials
