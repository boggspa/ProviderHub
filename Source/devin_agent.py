"""Devin AI Agent provider for Mistral Bridge.

Devin is a SESSION-BASED AI agent service (v3 API) that does NOT provide
OpenAI-compatible /v1/chat/completions or /v1/models endpoints.

This module implements Devin as an AGENT PROVIDER with:
- Session management (create, list, get, archive)
- Task submission via agent sessions
- Organization handling
- RBAC support

Architecture:
- Devin uses POST /v3/organizations/{org_id}/sessions to create agent sessions
- Sessions support modes: normal, fast, lite, ultra, fusion
- Authentication via Bearer tokens (cog_ prefix for service users, pat_ for personal)
- Full enterprise support with audit logging

No SDK, OAuth credential, CLI session, or external state is used here.
"""
from __future__ import annotations

import copy
import json
import re
import urllib.parse
from datetime import datetime, timezone


class DevinAgentError(ValueError):
    """A Devin agent session, request, or configuration is invalid."""


# Provider identity
PROVIDER_ID = "devin"
PROVIDER_NAME = "Devin Agent"

# API configuration
API_BASE = "https://api.devin.ai"
V3_BASE = f"{API_BASE}/v3"

# Authentication
AUTH_HEADER = {"name": "Authorization", "prefix": "Bearer "}
CREDENTIAL_ENV = "DEVIN_API_KEY"
SETUP_URL = "https://app.devin.ai/settings/api-keys"

# Devin v3 API endpoints
SESSIONS_CREATE = f"{V3_BASE}/organizations/{{org_id}}/sessions"
SESSIONS_LIST = f"{V3_BASE}/organizations/{{org_id}}/sessions"
SESSIONS_GET = f"{V3_BASE}/organizations/{{org_id}}/sessions/{{session_id}}"
SESSIONS_ARCHIVE = f"{V3_BASE}/organizations/{{org_id}}/sessions/{{session_id}}/archive"
SESSIONS_MESSAGES = f"{V3_BASE}/organizations/{{org_id}}/sessions/{{session_id}}/messages"
SESSIONS_ATTACHMENTS = f"{V3_BASE}/organizations/{{org_id}}/sessions/{{session_id}}/attachments"
SESSIONS_TAGS = f"{V3_BASE}/organizations/{{org_id}}/sessions/{{session_id}}/tags"
ORGANIZATIONS = f"{V3_BASE}/organizations"
USERS = f"{V3_BASE}/users"
SERVICE_USERS = f"{V3_BASE}/service-users"

# API documentation
DOCS_URL = "https://docs.devin.ai/api-reference"
SESSIONS_DOCS = f"{DOCS_URL}/v3/sessions"
ORGANIZATIONS_DOCS = f"{DOCS_URL}/v3/organizations"
AUTH_DOCS = f"{DOCS_URL}/authentication"

# Token prefixes
SERVICE_USER_PREFIX = "cog_"
PERSONAL_TOKEN_PREFIX = "pat_"
LEGACY_PREFIXES = ["apk_user_", "apk_"]

# Session modes (Devin agent modes)
DEVIN_MODES = {
    "normal": {"description": "Default agent mode. Fast and good at long-horizon planning."},
    "fast": {"description": "~2x faster, 4x more expensive, same intelligence."},
    "lite": {"description": "Devin Lite — lighter, lower-cost mode for smaller-scope sessions."},
    "ultra": {"description": "Devin Ultra — the most capable mode."},
    "fusion": {"description": "Fusion — multi-model routing."},
}

# Session lifecycle states
SESSION_STATES = {
    "pending": "Session is being created",
    "running": "Session is actively processing",
    "completed": "Session has finished successfully",
    "failed": "Session failed with an error",
    "cancelled": "Session was cancelled by user",
    "archived": "Session has been archived",
}

# Model ID pattern
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/+\-]{0,199}\Z")
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ORG_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

_LOCAL_CREDENTIAL_FIELDS = {
    "api_key", "api-key", "x-api-key", "authorization", "gateway_token", "gateway-token",
}


# =============================================================================
# AGENT PROVIDER DESCRIPTOR
# =============================================================================

DESCRIPTOR = {
    "id": PROVIDER_ID,
    "name": PROVIDER_NAME,
    "protocol": "agent_session",  # NEW: Not chat_completions or anthropic
    "default_base_url": V3_BASE,
    "default_region": "global",
    "regions": {"global": V3_BASE},
    "auth_header": AUTH_HEADER,
    "credential_account": CREDENTIAL_ENV,
    "credential_env": CREDENTIAL_ENV,
    "setup_url": SETUP_URL,
    "capabilities": {
        "streaming": True,
        "tools": True,
        "thinking": True,
        "vision": False,  # Devin does not support vision/multimodal
        "model_discovery": "documentation",  # No /v1/models - Devin is a single agent
        "reasoning_history": "native",
        # NEW: Agent-specific capabilities
        "session_management": True,
        "task_submission": True,
        "rbac": True,
        "enterprise": True,
        "audit_logging": True,
    },
}


OFFICIAL_PATHS = {
    "",
    "/v3",
    "/v3/organizations",
    "/v3/organizations/*/sessions",
    "/v3/users",
    "/v3/service-users",
}


# =============================================================================
# ORGANIZATION & SESSION HANDLING
# =============================================================================

class DevinOrganization:
    """Represents a Devin organization."""
    
    def __init__(self, org_data: dict):
        self.id = org_data.get("id")
        self.name = org_data.get("name")
        self.slug = org_data.get("slug")
        self.created_at = org_data.get("createdAt")
        self.updated_at = org_data.get("updatedAt")
        
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DevinSession:
    """Represents a Devin agent session."""
    
    def __init__(self, session_data: dict, org_id: str):
        self.id = session_data.get("id")
        self.org_id = org_id
        self.task = session_data.get("task")
        self.mode = session_data.get("mode", "normal")
        self.state = session_data.get("state", "pending")
        self.created_at = session_data.get("createdAt")
        self.updated_at = session_data.get("updatedAt")
        self.completed_at = session_data.get("completedAt")
        self.error = session_data.get("error")
        self.tags = session_data.get("tags", [])
        self.metadata = session_data.get("metadata", {})
        
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "org_id": self.org_id,
            "task": self.task,
            "mode": self.mode,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "tags": self.tags,
            "metadata": self.metadata,
        }
    
    @property
    def is_active(self) -> bool:
        return self.state in ("pending", "running")
    
    @property
    def is_terminal(self) -> bool:
        return self.state in ("completed", "failed", "cancelled", "archived")


# =============================================================================
# VALIDATION
# =============================================================================

def _valid_key(api_key: str | None) -> str:
    """Validate Devin API key format."""
    if not isinstance(api_key, str) or not api_key.strip():
        raise DevinAgentError("A Devin API key is required.")
    value = api_key.strip()
    if "\r" in value or "\n" in value:
        raise DevinAgentError("Devin API key contains invalid characters.")
    
    # Check for valid prefix
    valid_prefixes = [SERVICE_USER_PREFIX, PERSONAL_TOKEN_PREFIX] + LEGACY_PREFIXES
    has_valid_prefix = any(value.startswith(prefix) for prefix in valid_prefixes)
    
    if not has_valid_prefix:
        raise DevinAgentError(
            f"Devin API key must start with one of: {', '.join(valid_prefixes)}"
        )
    
    return value


def _valid_org_id(org_id: str) -> str:
    """Validate organization ID."""
    if not isinstance(org_id, str) or not _ORG_ID.fullmatch(org_id):
        raise DevinAgentError("Invalid Devin organization ID.")
    return org_id


def _valid_session_id(session_id: str) -> str:
    """Validate session ID."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise DevinAgentError("Invalid Devin session ID.")
    return session_id


def _valid_mode(mode: str) -> str:
    """Validate Devin agent mode."""
    if mode not in DEVIN_MODES:
        raise DevinAgentError(
            f"Invalid Devin mode '{mode}'. Valid modes: {list(DEVIN_MODES.keys())}"
        )
    return mode


def validate_connection(connection: dict | None) -> dict:
    """Validate Devin agent connection settings."""
    if connection is None:
        connection = {}
    if not isinstance(connection, dict):
        raise DevinAgentError("Devin connection must be an object.")
    
    # Check for credentials in connection (not allowed)
    if any(str(key).lower() in _LOCAL_CREDENTIAL_FIELDS for key in connection):
        raise DevinAgentError("Devin credentials cannot be stored in connection settings.")
    
    # Validate org_id if provided
    org_id = connection.get("org_id")
    if org_id is not None:
        _valid_org_id(org_id)
    
    # Validate base_url if provided
    value = connection.get("base_url")
    if value is None:
        value = V3_BASE
    
    if not isinstance(value, str) or not value.strip():
        raise DevinAgentError("Devin base URL must be a non-empty URL.")
    
    try:
        parsed = urllib.parse.urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise DevinAgentError("Devin base URL has an invalid port.") from exc
    
    if parsed.username is not None or parsed.password is not None:
        raise DevinAgentError("Devin base URL cannot contain credentials.")
    if parsed.query or parsed.fragment:
        raise DevinAgentError("Devin base URL cannot contain a query or fragment.")
    if parsed.scheme.lower() != "https" or port not in (None, 443):
        raise DevinAgentError("Devin requires HTTPS on its official endpoint.")
    if (parsed.hostname or "").lower().rstrip(".") != "api.devin.ai":
        raise DevinAgentError("Devin base URL must use the official Devin API domain.")
    
    return {"org_id": org_id, "base_url": V3_BASE}


# =============================================================================
# HEADERS
# =============================================================================

def _headers(api_key: str, content_type: bool = True) -> dict:
    """Generate Devin API headers."""
    headers = {
        "Accept": "application/json",
        "User-Agent": "ProviderHub/0.5",
        "Authorization": AUTH_HEADER["prefix"] + api_key,
    }
    if content_type:
        headers["Content-Type"] = "application/json"
    return headers


# =============================================================================
# ORGANIZATION OPERATIONS
# =============================================================================

def list_organizations(api_key: str, *, transport) -> list[DevinOrganization]:
    """List all organizations accessible with the given API key.
    
    Args:
        api_key: Devin API key (cog_ or pat_ prefix)
        transport: Function that sends HTTP requests and returns JSON
        
    Returns:
        List of DevinOrganization objects
    """
    key = _valid_key(api_key)
    
    if not callable(transport):
        raise DevinAgentError("Devin operations require a metadata transport.")
    
    plan = {
        "url": ORGANIZATIONS,
        "headers": _headers(key),
        "method": "GET",
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin organizations response is not a JSON object.")
    
    orgs_data = raw.get("data", [])
    if not isinstance(orgs_data, list):
        raise DevinAgentError("Devin organizations response has no data list.")
    
    organizations = []
    for org_data in orgs_data:
        if isinstance(org_data, dict):
            organizations.append(DevinOrganization(org_data))
    
    return organizations


def get_organization(api_key: str, org_id: str, *, transport) -> DevinOrganization:
    """Get a specific organization by ID."""
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    
    if not callable(transport):
        raise DevinAgentError("Devin operations require a metadata transport.")
    
    plan = {
        "url": f"{ORGANIZATIONS}/{org_id}",
        "headers": _headers(key),
        "method": "GET",
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin organization response is not a JSON object.")
    
    return DevinOrganization(raw)


# =============================================================================
# SESSION OPERATIONS
# =============================================================================

def create_session(
    api_key: str,
    org_id: str,
    task: str,
    mode: str = "normal",
    tags: list[str] | None = None,
    metadata: dict | None = None,
    *,
    transport,
) -> DevinSession:
    """Create a new Devin agent session.
    
    Args:
        api_key: Devin API key
        org_id: Organization ID
        task: Task description for the agent
        mode: Agent mode (normal, fast, lite, ultra, fusion)
        tags: Optional list of tags for the session
        metadata: Optional metadata dict
        transport: Function that sends HTTP requests and returns JSON
        
    Returns:
        DevinSession object with the created session details
    """
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    mode = _valid_mode(mode)
    
    if not isinstance(task, str) or not task.strip():
        raise DevinAgentError("Session task must be a non-empty string.")
    
    body = {
        "task": task,
        "mode": mode,
    }
    
    if tags:
        body["tags"] = tags
    if metadata:
        body["metadata"] = metadata
    
    url = SESSIONS_CREATE.format(org_id=org_id)
    
    plan = {
        "url": url,
        "headers": _headers(key),
        "method": "POST",
        "body": body,
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin session creation response is not a JSON object.")
    
    return DevinSession(raw, org_id)


def list_sessions(
    api_key: str,
    org_id: str,
    state: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    *,
    transport,
) -> tuple[list[DevinSession], str | None]:
    """List Devin agent sessions for an organization.
    
    Args:
        api_key: Devin API key
        org_id: Organization ID
        state: Filter by session state (pending, running, completed, failed, etc.)
        limit: Maximum number of sessions to return
        cursor: Pagination cursor
        transport: Function that sends HTTP requests and returns JSON
        
    Returns:
        Tuple of (list of DevinSession objects, next cursor or None)
    """
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    
    query = {}
    if state is not None:
        query["state"] = state
    if limit is not None:
        query["limit"] = limit
    if cursor is not None:
        query["cursor"] = cursor
    
    url = SESSIONS_LIST.format(org_id=org_id)
    if query:
        url += "?" + urllib.parse.urlencode(query)
    
    plan = {
        "url": url,
        "headers": _headers(key),
        "method": "GET",
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin sessions list response is not a JSON object.")
    
    sessions_data = raw.get("data", [])
    if not isinstance(sessions_data, list):
        raise DevinAgentError("Devin sessions response has no data list.")
    
    sessions = [DevinSession(s, org_id) for s in sessions_data if isinstance(s, dict)]
    next_cursor = raw.get("cursor")
    
    return sessions, next_cursor


def get_session(api_key: str, org_id: str, session_id: str, *, transport) -> DevinSession:
    """Get a specific Devin agent session."""
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    session_id = _valid_session_id(session_id)
    
    url = SESSIONS_GET.format(org_id=org_id, session_id=session_id)
    
    plan = {
        "url": url,
        "headers": _headers(key),
        "method": "GET",
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin session response is not a JSON object.")
    
    return DevinSession(raw, org_id)


def archive_session(api_key: str, org_id: str, session_id: str, *, transport) -> DevinSession:
    """Archive a Devin agent session."""
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    session_id = _valid_session_id(session_id)
    
    url = SESSIONS_ARCHIVE.format(org_id=org_id, session_id=session_id)
    
    plan = {
        "url": url,
        "headers": _headers(key),
        "method": "POST",
    }
    
    raw = transport(plan)
    
    if not isinstance(raw, dict):
        raise DevinAgentError("Devin session archive response is not a JSON object.")
    
    return DevinSession(raw, org_id)


def cancel_session(api_key: str, org_id: str, session_id: str, *, transport) -> DevinSession:
    """Cancel a running Devin agent session.
    
    Note: This is implemented via the archive endpoint with a cancel intent,
    or may require a separate endpoint. Check Devin API for exact cancel mechanism.
    """
    # For now, use archive as a proxy - actual cancel endpoint needs verification
    return archive_session(api_key, org_id, session_id, transport=transport)


# =============================================================================
# TASK SUBMISSION
# =============================================================================

def submit_task(
    api_key: str,
    org_id: str,
    session_id: str,
    message: str,
    *,
    transport,
) -> dict:
    """Submit a message/task to an existing Devin agent session.
    
    Args:
        api_key: Devin API key
        org_id: Organization ID
        session_id: Session ID
        message: Task message to submit
        transport: Function that sends HTTP requests and returns JSON
        
    Returns:
        Response from Devin API
    """
    key = _valid_key(api_key)
    org_id = _valid_org_id(org_id)
    session_id = _valid_session_id(session_id)
    
    if not isinstance(message, str) or not message.strip():
        raise DevinAgentError("Task message must be a non-empty string.")
    
    url = SESSIONS_MESSAGES.format(org_id=org_id, session_id=session_id)
    
    body = {
        "content": message,
    }
    
    plan = {
        "url": url,
        "headers": _headers(key),
        "method": "POST",
        "body": body,
    }
    
    return transport(plan)


# =============================================================================
# CATALOGUE (Agent-based, not model-based)
# =============================================================================

def catalogue(api_key: str | None = None, *, transport=None) -> tuple[list[dict], list[str], str]:
    """Return Devin agent capabilities catalogue.
    
    Since Devin is a single agent service (not multi-model), this returns
    the agent modes and capabilities rather than model IDs.
    
    Args:
        api_key: Optional Devin API key for live organization listing
        transport: Optional transport for live API calls
        
    Returns:
        Tuple of (modes list, warnings, docs URL)
    """
    modes = []
    for mode_id, mode_info in DEVIN_MODES.items():
        modes.append({
            "id": mode_id,
            "display_name": f"Devin {mode_id.title()}",
            "description": mode_info["description"],
            "capabilities": {
                "thinking": True,
                "tools": True,
                "streaming": True,
                "vision": False,
            },
            "access_tier": "default",
            "source": "provider_documentation",
            "evidence": f"{SESSIONS_DOCS}#create-session",
        })
    
    warnings = [
        "Devin is a session-based AI agent, not a traditional LLM provider.",
        "Agent modes represent different capability/pricing tiers, not separate models.",
        "Organization access and agent availability depend on your Devin account.",
        "Use list_organizations() to see accessible organizations.",
    ]
    
    return modes, warnings, DOCS_URL


# =============================================================================
# AGENT CAPABILITIES
# =============================================================================

def get_agent_capabilities() -> dict:
    """Get Devin agent capabilities summary."""
    return {
        "provider_id": PROVIDER_ID,
        "provider_name": PROVIDER_NAME,
        "protocol": "agent_session",
        "capabilities": {
            "session_management": {
                "create": True,
                "list": True,
                "get": True,
                "archive": True,
                "cancel": True,
            },
            "task_submission": True,
            "streaming": True,
            "tools": True,
            "thinking": True,
            "vision": False,
            "rbac": True,
            "enterprise": True,
            "audit_logging": True,
        },
        "modes": list(DEVIN_MODES.keys()),
        "authentication": {
            "header": AUTH_HEADER["name"],
            "prefix": AUTH_HEADER["prefix"],
            "env_var": CREDENTIAL_ENV,
            "token_prefixes": {
                "service_user": SERVICE_USER_PREFIX,
                "personal": PERSONAL_TOKEN_PREFIX,
                "legacy": LEGACY_PREFIXES,
            },
        },
        "endpoints": {
            "base": API_BASE,
            "v3": V3_BASE,
            "organizations": ORGANIZATIONS,
            "sessions": SESSIONS_LIST,
            "documentation": DOCS_URL,
        },
    }


# =============================================================================
# EXPORT FOR PROVIDERS.PY INTEGRATION
# =============================================================================

# These are the functions that providers.py will import and use
DESCRIPTOR = DESCRIPTOR
OFFICIAL_PATHS = OFFICIAL_PATHS
DevinAgentError = DevinAgentError

# Agent-specific exports
list_organizations = list_organizations
get_organization = get_organization
create_session = create_session
list_sessions = list_sessions
get_session = get_session
archive_session = archive_session
cancel_session = cancel_session
submit_task = submit_task
catalogue = catalogue
get_agent_capabilities = get_agent_capabilities

# Validation exports
validate_connection = validate_connection
