# Devin Provider Integration Guide

## Analysis Summary

This document describes the implementation pattern for adding Devin AI support to Mistral Bridge, based on analysis of existing providers (Gemini, Cerebras, Qwen, OpenRouter).

## Implementation Pattern Analysis

### 1. DESCRIPTOR Structure (for chat_completions providers)

```python
DESCRIPTOR = {
    "id": "devin",                    # Provider identifier
    "name": "Devin AI",               # Human-readable name
    "protocol": "chat_completions",    # API protocol type
    "default_base_url": "https://api.devin.ai/v1",
    "default_region": "global",
    "regions": {"global": "https://api.devin.ai/v1"},
    "auth_header": {"name": "Authorization", "prefix": "Bearer "},
    "credential_account": "DEVIN_API_KEY",
    "credential_env": "DEVIN_API_KEY",
    "setup_url": "https://app.devin.ai/settings/api-keys",
    "capabilities": {
        "streaming": True,
        "tools": True,
        "thinking": True,
        "vision": "model_dependent",
        "model_discovery": "api",
        "reasoning_history": "gateway_signed_replay",
    },
}
```

### 2. OFFICIAL_PATHS Structure

```python
OFFICIAL_PATHS = {
    "",
    "/v1",
    "/v1/models",
    "/v1/chat/completions",
}
```

### 3. Required Exported Functions

From analysis of existing providers, a chat_completions provider module MUST export:

- **DESCRIPTOR**: Provider metadata dict (required)
- **OFFICIAL_PATHS**: Set of valid URL paths (required)
- **<Provider>Error**: Custom error class (required)
- **discover()**: Model discovery function (optional, for API-based discovery)
- **catalogue()**: Static catalogue function (optional, for documentation-based discovery)
- **prepare_request()**: Request preparation function (required for gateway)
- **validate_connection()**: Connection validation (required for gateway)
- **normalize_controls()**: Payload normalization (required for gateway)
- **usage_counts()**: Token usage conversion (optional but recommended)

### 4. Model Catalogue Structure

Each model entry must contain:
- `id`: Model identifier
- `canonical_id`: Base model identifier
- `display_name`: Human-readable name
- `aliases`: List of model aliases
- `context`: Input token limit
- `max_input`: Maximum input tokens
- `max_output`: Maximum output tokens
- `context_kind`: Source of context limit
- `tools`: Boolean for tool support
- `vision`: Boolean or "model_dependent" for vision support
- `reasoning`: Boolean for reasoning support
- `effort_modes`: List of supported effort levels
- `default_effort`: Default effort level
- `provider_effort_modes`: Provider-specific effort modes
- `fast_mode`: Boolean for fast mode support
- `inference_status`: Model availability status
- `source`: Catalogue source
- `evidence`: Documentation URL
- `reasoning_history`: Reasoning history support
- `complete_tool_cycles`: Boolean for complete tool cycles
- `streaming`: Boolean for streaming support
- `parallel_tool_calls`: Boolean for parallel tool calls

### 5. Reasoning Handling Pattern (from cerebras_replay.py)

Devin uses gateway-signed replay for reasoning history:
- Reasoning content is signed by the gateway
- Signatures are verified on replay
- Uses `sign_thinking()` from cerebras_replay pattern
- Reasoning blocks are attached as Anthropic thinking blocks

### 6. Tool Handling Pattern

Devin uses standard OpenAI chat_completions tool format:
- Tools are passed in the request body
- Tool calls are returned in assistant messages
- Tool name mapping is handled by `translate_chat()`

## Files Created

### 1. Source/devin_provider.py

Complete implementation with:
- DESCRIPTOR for Devin provider metadata
- OFFICIAL_PATHS for valid API endpoints
- DevinError custom exception class
- validate_connection() for connection validation
- discover() for live model discovery via API
- catalogue() for static documentation-based catalogue
- prepare_request() for request preparation
- normalize_controls() for payload normalization
- usage_counts() for token usage conversion
- _normalize_effort() for effort level normalization

### 2. Changes Required to providers.py

#### Import Addition (after line 208):
```python
from devin_provider import (
    DESCRIPTOR as DEVIN_DESCRIPTOR,
    OFFICIAL_PATHS as DEVIN_PATHS,
    DevinError,
    catalogue as devin_catalogue,
    normalize_controls as devin_controls,
    discover as devin_discover,
    prepare_request as devin_prepare_request,
    validate_connection as devin_validate_connection,
)
```

#### PROVIDERS Dict Addition (after line 211):
```python
PROVIDERS[DEVIN_DESCRIPTOR["id"]] = DEVIN_DESCRIPTOR
```

#### _OFFICIAL_PATHS Addition (after line 223):
```python
    "devin": DEVIN_PATHS,
```

#### _static_catalogue Function Addition (around line 632):
```python
    if provider_id == "devin":
        return devin_catalogue()
```

#### discover() Function Addition (around line 1177):
```python
    if provider_id == "devin":
        try:
            return devin_discover(normalized, api_key, transport=transport or _fetch_json)
        except DevinError as exc:
            raise ProviderError(str(exc)) from exc
```

### 3. Test Files to Create

#### test_devin_provider.py
- Test DESCRIPTOR structure
- Test OFFICIAL_PATHS validation
- Test validate_connection() with valid/invalid URLs
- Test _catalogue_entry() with various model cards
- Test discover() with mock transport
- Test prepare_request() with various payloads
- Test normalize_controls() with various effort levels
- Test usage_counts() with various usage formats

#### test_devin_gateway.py
- Test end-to-end gateway integration
- Test request routing
- Test response handling
- Test streaming
- Test tool calls
- Test reasoning replay

## Key Design Decisions

### 1. Protocol Choice
- Devin uses OpenAI-compatible chat_completions API
- Protocol set to "chat_completions" matching Mistral, Cerebras, Grok

### 2. Reasoning History
- Uses "gateway_signed_replay" matching Cerebras pattern
- Requires gateway integration for signing/verification

### 3. Model Discovery
- Primary: API-based via discover() function
- Fallback: Static catalogue via catalogue() function
- Follows same pattern as Gemini and OpenRouter

### 4. Effort Mapping
- Supports: none, low, medium, high
- Default: medium for all models
- Maps xhigh/max/ultra to high

### 5. Capabilities
- streaming: True (Devin supports streaming)
- tools: True (Devin supports function calling)
- thinking: True (Devin supports reasoning)
- vision: model_dependent (varies by model)
- model_discovery: api (Devin has /v1/models endpoint)
- reasoning_history: gateway_signed_replay

## Integration Checklist

- [x] Create Source/devin_provider.py
- [ ] Add import to providers.py
- [ ] Add to PROVIDERS dict
- [ ] Add to _OFFICIAL_PATHS
- [ ] Add to _static_catalogue
- [ ] Add to discover() function
- [ ] Create test_devin_provider.py
- [ ] Create test_devin_gateway.py
- [ ] Add DEVIN_API_KEY to credential management
- [ ] Add Devin logo to provider-logos/
- [ ] Update provider_branding.json
- [ ] Test end-to-end integration

## Testing Strategy

1. **Unit Tests**: Test each function in isolation
2. **Integration Tests**: Test provider with gateway
3. **Manual Tests**: Test with real Devin API key
4. **Edge Cases**: Invalid URLs, missing credentials, rate limits

## References

- Gemini provider: Best reference for chat_completions pattern
- Cerebras provider: Reference for reasoning replay
- Qwen provider: Reference for static catalogue
- OpenRouter provider: Reference for API-based discovery
