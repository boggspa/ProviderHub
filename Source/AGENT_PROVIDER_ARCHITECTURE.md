# Agent Provider Architecture for Mistral Bridge

## Executive Summary

**CRITICAL PIVOT**: Devin is NOT an LLM chat_completions provider. Devin is a **SESSION-BASED AI AGENT SERVICE** with a v3 API that uses `POST /v3/organizations/{org_id}/sessions` for task execution.

This requires a **NEW ARCHITECTURE** for agent providers, separate from the existing LLM provider system.

## Current Architecture Analysis

### Existing Provider System (providers.py)

The current Mistral Bridge has **LLM-focused providers** with two protocols:

1. **`chat_completions`** (Mistral, Cerebras, Grok)
   - OpenAI-compatible REST API
   - Stateless request/response
   - Streaming support
   - Tool calling via function schema

2. **`anthropic`** (Kimi, MiMo, Ollama, DeepSeek, Muse)
   - Anthropic Messages API
   - Stateless request/response
   - Streaming support
   - Tool calling via Messages API

### Gateway Integration (gateway.py)

The gateway handles:
- `POST /v1/messages` - Anthropic protocol
- `POST /v1/messages/count_tokens` - Token counting
- `POST /v1/responses` - Responses API

All routes assume **stateless LLM inference**, not session-based agents.

## Required Architecture Changes

### Option 1: Separate Agent Gateway (RECOMMENDED)

Create a **parallel agent gateway** with new endpoints:

```
POST /v1/agents/sessions          - Create agent session
GET  /v1/agents/sessions          - List sessions
GET  /v1/agents/sessions/{id}      - Get session status
POST /v1/agents/sessions/{id}/cancel - Cancel session
POST /v1/agents/tasks              - Submit task (creates session)
```

**Pros:**
- Clean separation of concerns
- No breaking changes to existing LLM infrastructure
- Can evolve independently
- Clear API contract

**Cons:**
- New gateway endpoints
- Requires UI changes for agent-specific flows

### Option 2: Extended Provider Protocol

Add new protocol type to existing provider system:

```python
PROVIDERS = {
    "mistral": {"protocol": "chat_completions", ...},
    "devin": {"protocol": "agent_sessions", ...},  # NEW
}
```

**Pros:**
- Reuses existing provider registry
- Minimal gateway changes

**Cons:**
- Mixes stateless and stateful paradigms
- Gateway needs significant refactoring
- Confusing for users

## Recommended Architecture: Option 1

### 1. New Agent Provider Registry

```python
# agents.py (NEW FILE)

AGENT_PROVIDERS = {
    "devin": {
        "id": "devin",
        "name": "Devin AI",
        "protocol": "agent_sessions",
        "base_url": "https://api.devin.ai/v3",
        "auth_header": {"name": "Authorization", "prefix": "Bearer "},
        "credential_env": "DEVIN_API_KEY",
        "capabilities": {
            "session_management": True,
            "async_execution": True,
            "task_submission": True,
            "state_tracking": True,
        },
        "requires_org_id": True,  # Devin needs org_id
    },
}
```

### 2. Agent Gateway (NEW)

```python
# agent_gateway.py (NEW FILE)

class AgentHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        path = urlparse(self.path).path
        
        if path == "/v1/agents/sessions":
            return self.handle_create_session()
        if path == "/v1/agents/tasks":
            return self.handle_submit_task()
        if path.startswith("/v1/agents/sessions/"):
            return self.handle_session_operation()
        
        self.error(404, "Unknown agent endpoint")
    
    def handle_create_session(self):
        # Parse request
        # Validate org_id
        # Create session via agent provider
        # Return session_id
        pass
    
    def handle_submit_task(self):
        # Parse task request
        # Create or use existing session
        # Return session for tracking
        pass
    
    def handle_session_operation(self):
        # GET: status
        # POST /cancel: cancel
        # DELETE: delete/archive
        pass
```

### 3. Agent Runtime

```python
# agent_runtime.py (NEW FILE)

class AgentRuntime:
    """Manages agent sessions and their lifecycle."""
    
    def __init__(self):
        self.sessions = {}  # session_id -> AgentSession
        self.org_sessions = defaultdict(dict)  # org_id -> {session_id -> AgentSession}
        self.lock = threading.Lock()
        self.active_sessions = 0
        self.max_concurrent = 5  # Devin limit
    
    def create_session(self, provider_id, org_id, task, **kwargs):
        """Create a new agent session."""
        # Call provider-specific create_session
        # Track in self.sessions
        # Return session
        pass
    
    def get_session(self, provider_id, org_id, session_id):
        """Get session status."""
        pass
    
    def cancel_session(self, provider_id, org_id, session_id):
        """Cancel a running session."""
        pass
    
    def cleanup_sessions(self):
        """Clean up completed/failed sessions."""
        pass
```

### 4. Agent Provider Interface

```python
# agent_providers.py (NEW FILE)

class AgentProvider(ABC):
    """Abstract base class for agent providers."""
    
    @abstractmethod
    def create_session(self, org_id, task, **kwargs) -> AgentSession:
        pass
    
    @abstractmethod
    def get_session(self, org_id, session_id) -> AgentSession:
        pass
    
    @abstractmethod
    def list_sessions(self, org_id, **kwargs) -> list[AgentSession]:
        pass
    
    @abstractmethod
    def cancel_session(self, org_id, session_id) -> AgentSession:
        pass
    
    @abstractmethod
    def validate_connection(self, connection) -> dict:
        pass


class DevinAgentProvider(AgentProvider):
    """Devin-specific agent provider implementation."""
    
    # Implement all abstract methods
    pass
```

### 5. Devin Agent Provider

See `devin_agent.py` for the complete implementation.

## Devin-Specific Design

### API Mapping

| Mistral Bridge Concept | Devin v3 API |
|------------------------|--------------|
| Agent Provider | `devin` |
| Session | Devin Session |
| Task | Session task |
| Organization | Devin Organization |
| Session ID | Session UUID |
| State | `pending`, `running`, `completed`, `failed`, `cancelled` |

### Endpoint Mapping

```
Mistral Bridge Agent API          Devin v3 API
-------------------------------   ----------------------------
POST /v1/agents/sessions           POST /v3/orgs/{org}/sessions
GET  /v1/agents/sessions           GET  /v3/orgs/{org}/sessions
GET  /v1/agents/sessions/{id}      GET  /v3/orgs/{org}/sessions/{id}
POST /v1/agents/sessions/{id}/cancel POST /v3/orgs/{org}/sessions/{id}/cancel
```

### Session Lifecycle

```
                    ┌─────────────┐
                    │   PENDING   │◄─────────────┐
                    └──────┬──────┘              │
                           │ create_session()   │ cancel
                           ▼                    │
                    ┌─────────────┐              │
   submit_task() ──► │   RUNNING   │──────────────┘
                    └──────┬──────┘
                           │
         ┌─────────────────┼─────────────────┐
         ▼                 ▼                 ▼
  ┌──────────┐     ┌──────────┐         ┌──────────┐
  │ COMPLETED │     │  FAILED  │         │ CANCELLED│
  └──────────┘     └──────────┘         └──────────┘
```

### Required Parameters

**Connection Settings:**
- `base_url`: API base URL (default: `https://api.devin.ai/v3`)
- `org_id`: Organization ID (REQUIRED for all operations)

**Session Creation:**
- `task` (required): Natural language task description
- `model` (optional): Model to use
- `workspace` (optional): Workspace context
- `environment` (optional): Environment variables
- `timeout_seconds` (optional): Session timeout (default: 3600)
- `metadata` (optional): Custom metadata

## Integration Points

### 1. providers.py Changes

Add Devin as a special agent provider:

```python
# In providers.py

# Add to PROVIDERS dict (but mark as agent protocol)
PROVIDERS["devin"] = {
    "id": "devin",
    "name": "Devin AI",
    "protocol": "agent_sessions",  # NEW protocol type
    "default_base_url": "https://api.devin.ai/v3",
    "default_region": "global",
    "regions": {"global": "https://api.devin.ai/v3"},
    "auth_header": {"name": "Authorization", "prefix": "Bearer "},
    "credential_account": "DEVIN_API_KEY",
    "credential_env": "DEVIN_API_KEY",
    "setup_url": "https://app.devin.ai/settings/api-keys",
    "capabilities": {
        "streaming": False,
        "tools": True,
        "thinking": True,
        "vision": "model_dependent",
        "model_discovery": "documentation",
        "reasoning_history": "not_applicable",
        # Agent-specific
        "session_management": True,
        "async_execution": True,
        "requires_org_id": True,
    },
}

# Add to _OFFICIAL_PATHS
_OFFICIAL_PATHS["devin"] = {"", "/v3", "/v3/organizations", "/v3/organizations/{org_id}/sessions"}
```

### 2. gateway.py Changes

Add new agent endpoints:

```python
# In gateway.py Handler class

def do_POST(self):
    # ... existing code ...
    
    # NEW: Agent endpoints
    if path == "/v1/agents/sessions":
        return self.handle_agent_sessions()
    if path == "/v1/agents/tasks":
        return self.handle_agent_tasks()
    if path.startswith("/v1/agents/sessions/"):
        return self.handle_agent_session_operation()
    
    # ... rest of existing code ...

def handle_agent_sessions(self):
    # Create new agent session
    pass

def handle_agent_tasks(self):
    # Submit task to agent
    pass

def handle_agent_session_operation(self):
    # GET, POST/cancel, DELETE on session
    pass
```

### 3. Bridge Core Changes

Update `bridge_core.py` to support agent providers:

```python
# In bridge_core.py

def load_settings(root):
    # ... existing code ...
    
    # Add agent provider support
    settings["_agent_providers"] = {}
    settings["_agent_specs"] = {}
    
    return settings
```

### 4. UI Integration Strategy

**Option A: Separate "Agents" Section** (RECOMMENDED)

```
┌─────────────────────────────────────────┐
│  Mistral Bridge                           │
├─────────────────────────────────────────┤
│  Providers          Agents     Settings   │
│                                             │
│  [LLM Providers]    [Agent Providers]      │
│  - Mistral          - Devin                │
│  - Gemini           - (future agents)      │
│  - Cerebras                               │
│  - etc.                                     │
└─────────────────────────────────────────┘
```

**Agent-Specific UI:**
- Session list with status indicators
- Task submission form
- Session logs/output
- Cancel/Archive buttons

**Option B: Integrated Provider List**

Show both LLM and agent providers in one list with badges:
```
- Mistral (LLM)
- Gemini (LLM)
- Devin (Agent)  ← Badge indicates type
- Cerebras (LLM)
```

### 5. Desktop Harness Integration

For Codex desktop integration:

```swift
// In MistralBridge.swift or similar

class AgentSessionManager {
    func createSession(provider: String, orgId: String, task: String, completion: @escaping (Result<AgentSession, Error>) -> Void)
    func getSessionStatus(provider: String, orgId: String, sessionId: String, completion: @escaping (Result<AgentSession, Error>) -> Void)
    func cancelSession(provider: String, orgId: String, sessionId: String, completion: @escaping (Result<AgentSession, Error>) -> Void)
}
```

## Session Lifecycle Management

### Session States

```python
class SessionState(Enum):
    PENDING = "pending"      # Created, waiting to start
    RUNNING = "running"      # Actively executing
    COMPLETED = "completed"  # Finished successfully
    FAILED = "failed"        # Failed with error
    CANCELLED = "cancelled"  # User cancelled
    ARCHIVED = "archived"    # Read-only historical
```

### State Transitions

| From | To | Trigger |
|------|-----|---------|
| PENDING | RUNNING | Session starts |
| RUNNING | COMPLETED | Task completes |
| RUNNING | FAILED | Task fails |
| RUNNING | CANCELLED | User cancels |
| PENDING | CANCELLED | User cancels |
| COMPLETED | ARCHIVED | Manual archive |
| FAILED | ARCHIVED | Manual archive |

### Session Metadata

```python
@dataclass
class AgentSession:
    session_id: str
    provider_id: str
    org_id: str
    state: SessionState
    task: str
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    error: str | None
    result: dict | None
    steps: list[AgentStep]  # Execution steps
    logs: list[str]  # Session logs
    metadata: dict  # Custom metadata
```

### Step Tracking

```python
@dataclass
class AgentStep:
    step_id: str
    name: str
    description: str
    state: StepState  # pending, running, completed, failed
    started_at: datetime | None
    completed_at: datetime | None
    error: str | None
    result: Any | None
    tool_calls: list[ToolCall]
```

## Testing Strategy

### Unit Tests

1. **devin_agent.py tests:**
   - `test_validate_connection` - Valid/invalid URLs, org_id
   - `test_session_lifecycle` - State transitions
   - `test_create_session` - Session creation
   - `test_get_session` - Session retrieval
   - `test_list_sessions` - Session listing
   - `test_cancel_session` - Session cancellation
   - `test_submit_task` - Task submission
   - `test_catalogue` - Static catalogue

2. **Agent provider tests:**
   - `test_agent_provider_interface` - Interface compliance
   - `test_session_management` - CRUD operations
   - `test_error_handling` - Error scenarios

### Integration Tests

1. **Agent gateway tests:**
   - `test_create_session_endpoint`
   - `test_get_session_endpoint`
   - `test_list_sessions_endpoint`
   - `test_cancel_session_endpoint`
   - `test_submit_task_endpoint`

2. **End-to-end tests:**
   - `test_full_session_lifecycle`
   - `test_concurrent_sessions`
   - `test_session_timeout`

### Manual Tests

1. **With real Devin API:**
   - Create session
   - Submit task
   - Monitor progress
   - Get results
   - Cancel session

## Files to Create/Modify

### NEW FILES

1. **Source/agents.py** - Agent provider registry
2. **Source/agent_providers.py** - Agent provider base class
3. **Source/agent_runtime.py** - Agent runtime management
4. **Source/agent_gateway.py** - Agent gateway (HTTP handler)
5. **Source/devin_agent.py** - Devin agent provider (DONE)
6. **Source/test_devin_agent.py** - Unit tests
7. **Source/test_agent_gateway.py** - Gateway tests

### MODIFIED FILES

1. **Source/providers.py** - Add Devin as agent provider
2. **Source/gateway.py** - Add agent endpoints
3. **Source/bridge_core.py** - Add agent support
4. **Source/MistralBridge.swift** - Add agent session management
5. **Source/ProviderViews.swift** - Add agent UI

## Migration Path

### Phase 1: Core Infrastructure (Current)
- [x] Create devin_agent.py
- [ ] Design agent architecture
- [ ] Create agents.py registry
- [ ] Create agent_providers.py base class
- [ ] Create agent_runtime.py

### Phase 2: Gateway Integration
- [ ] Create agent_gateway.py
- [ ] Modify gateway.py to add agent endpoints
- [ ] Modify bridge_core.py for agent support
- [ ] Add agent session management

### Phase 3: UI Integration
- [ ] Add agent provider to UI
- [ ] Create agent session views
- [ ] Add task submission interface
- [ ] Add session monitoring

### Phase 4: Testing & Polish
- [ ] Unit tests for all components
- [ ] Integration tests
- [ ] Manual testing with Devin API
- [ ] Documentation

## Key Challenges

1. **State Management**: Agent sessions are stateful, unlike stateless LLM inference
2. **Asynchronous Execution**: Need to handle async session lifecycle
3. **Organization Scoping**: Devin requires org_id for all operations
4. **Rate Limiting**: Devin has concurrent session limits
5. **Error Handling**: Session failures need proper error propagation
6. **UI Integration**: Need new UI paradigms for agent workflows

## Recommendations

1. **Keep LLM and Agent providers separate** - They have fundamentally different paradigms
2. **Use new endpoints** - Don't try to fit agents into /v1/messages
3. **Embrace async** - Agent sessions are inherently asynchronous
4. **Track state explicitly** - Sessions have lifecycle, not just request/response
5. **Handle org_id carefully** - It's required for all Devin operations
6. **Respect rate limits** - Devin has concurrent session limits

## Next Steps

1. Review this architecture with the team
2. Get approval on the approach
3. Implement agent_gateway.py
4. Modify gateway.py to add agent endpoints
5. Create test infrastructure
6. Test with real Devin API
