"""JWT Authentication Middleware for MCP Server."""

import os
from typing import Awaitable, Callable, Optional

import jwt
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class JWTAuthMiddleware(BaseHTTPMiddleware):
    """Middleware to validate JWT tokens for all API requests except excluded paths."""

    def __init__(self, app: FastAPI, excluded_paths: Optional[list[str]] = None):
        """Initialize JWT Auth Middleware.

        Args:
            app: FastAPI application instance
            excluded_paths: List of paths to exclude from JWT validation (e.g., ["/health"])
        """
        super().__init__(app)
        self.excluded_paths = excluded_paths or ["/health"]
        self.secret_key = os.getenv("JWT_SECRET_KEY")
        self.algorithm = os.getenv("JWT_ALGORITHM", "HS256")

        if not self.secret_key:
            raise ValueError("JWT_SECRET_KEY environment variable is required")

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Process the request and validate JWT token.

        Args:
            request: Incoming HTTP request
            call_next: Next middleware or route handler

        Returns:
            Response from the next handler or error response
        """
        # Skip JWT validation for excluded paths
        if request.url.path in self.excluded_paths:
            return await call_next(request)

        # Extract token from Authorization header
        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authorization header missing"},
            )

        # Check for Bearer token format
        parts = auth_header.split()
        if len(parts) != 2 or parts[0].lower() != "bearer":
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid authorization header format. Expected: Bearer <token>"},
            )

        token = parts[1]

        try:
            # Verify and decode the JWT token
            payload = jwt.decode(
                token,
                self.secret_key,
                algorithms=[self.algorithm],
            )

            # Attach decoded payload to request state for use in route handlers
            request.state.jwt_payload = payload
            request.state.user_id = payload.get("user_id")
            request.state.user_email = payload.get("email")

            # Proceed to the next middleware or route handler
            response = await call_next(request)
            return response

        except jwt.ExpiredSignatureError:
            return JSONResponse(
                status_code=401,
                content={"detail": "Token has expired"},
            )
        except jwt.InvalidTokenError as e:
            return JSONResponse(
                status_code=401,
                content={"detail": f"Invalid token: {str(e)}"},
            )
        except Exception as e:
            return JSONResponse(
                status_code=500,
                content={"detail": f"Token validation error: {str(e)}"},
            )
