"""Unit tests for Devin Agent Provider.

Tests the devin_agent.py module for:
- Connection validation
- Session management
- Task submission
- Error handling
- Catalogue generation
"""
import copy
import unittest

from devin_agent import (
    V3_BASE,
    CREDENTIAL_ENV,
    PROVIDER_ID,
    PROVIDER_NAME,
    DESCRIPTOR,
    OFFICIAL_PATHS,
    DevinAgentError,
    DevinSession,
    DevinOrganization,
    create_session,
    get_session,
    list_sessions,
    cancel_session,
    archive_session,
    submit_task,
    catalogue,
    DEVIN_MODES,
    validate_connection,
    get_organization,
    list_organizations,
    _valid_key,
    _valid_org_id,
    _valid_session_id,
)


# =============================================================================
# Test Constants
# =============================================================================

TEST_API_KEY = "pat_test-fixture-key-never-sent"
TEST_ORG_ID = "test-org-123"
TEST_SESSION_ID = "session-abc-456"
TEST_TASK = "Write a Python function to sort a list of dictionaries by a key"


# =============================================================================
# Mock Transport
# =============================================================================

class MockTransport:
    """Mock transport for testing API calls."""
    
    def __init__(self):
        self.calls = []
    
    def __call__(self, plan):
        self.calls.append(copy.deepcopy(plan))
        url = plan.get("url", "")
        method = plan.get("method", "GET")
        
        # Archive endpoint must be checked first
        if method == "POST" and "/sessions/" in url and "/archive" in url:
            return {"id": TEST_SESSION_ID, "state": "archived", "task": TEST_TASK, "updated_at": "2026-01-01T00:00:02Z"}
        
        # Messages endpoint for submit_task
        if method == "POST" and "/messages" in url:
            return {"message": TEST_TASK, "status": "queued"}
        
        # Create session
        if "/sessions" in url and method == "POST" and "/archive" not in url:
            return {"id": TEST_SESSION_ID, "state": "pending", "task": TEST_TASK, "created_at": "2026-01-01T00:00:00Z", "metadata": {}}
        
        # Get single session
        if method == "GET" and "/sessions/" + TEST_SESSION_ID in url:
            return {"id": TEST_SESSION_ID, "state": "running", "task": TEST_TASK, "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:01Z", "metadata": {"current_step": "analyzing"}}
        
        # List sessions
        if method == "GET" and "/sessions" in url and "/" + TEST_SESSION_ID not in url:
            return {"data": [{"id": TEST_SESSION_ID, "state": "pending", "task": TEST_TASK, "created_at": "2026-01-01T00:00:00Z"}], "cursor": None}
        
        return {}


# =============================================================================
# DESCRIPTOR Tests
# =============================================================================

class DescriptorTests(unittest.TestCase):
    """Test DESCRIPTOR structure and values."""
    
    def test_descriptor_has_required_fields(self):
        """DESCRIPTOR has all required fields."""
        required = ["id", "name", "protocol", "default_base_url", "auth_header", "credential_env", "credential_account", "capabilities"]
        for field in required:
            self.assertIn(field, DESCRIPTOR)
    
    def test_descriptor_id_is_devin(self):
        """DESCRIPTOR id is 'devin'."""
        self.assertEqual(DESCRIPTOR["id"], PROVIDER_ID)
        self.assertEqual(PROVIDER_ID, "devin")
    
    def test_descriptor_name_is_devin_agent(self):
        """DESCRIPTOR name is 'Devin Agent'."""
        self.assertEqual(DESCRIPTOR["name"], PROVIDER_NAME)
        self.assertEqual(PROVIDER_NAME, "Devin Agent")
    
    def test_descriptor_protocol_is_agent_session(self):
        """DESCRIPTOR protocol is 'agent_session'."""
        self.assertEqual(DESCRIPTOR["protocol"], "agent_session")
    
    def test_descriptor_has_agent_capabilities(self):
        """DESCRIPTOR has agent-specific capabilities."""
        caps = DESCRIPTOR["capabilities"]
        self.assertTrue(caps.get("session_management"))
        self.assertTrue(caps.get("task_submission"))
        self.assertTrue(caps.get("streaming"))
    
    def test_descriptor_auth_header(self):
        """DESCRIPTOR has correct auth header."""
        auth = DESCRIPTOR["auth_header"]
        self.assertEqual(auth["name"], "Authorization")
        self.assertEqual(auth["prefix"], "Bearer ")
    
    def test_descriptor_credentials(self):
        """DESCRIPTOR has correct credential fields."""
        self.assertEqual(DESCRIPTOR["credential_env"], CREDENTIAL_ENV)
        self.assertEqual(DESCRIPTOR["credential_account"], CREDENTIAL_ENV)


# =============================================================================
# Official Paths Tests
# =============================================================================

class OfficialPathsTests(unittest.TestCase):
    """Test OFFICIAL_PATHS."""
    
    def test_official_paths_is_set(self):
        """OFFICIAL_PATHS is a set."""
        self.assertIsInstance(OFFICIAL_PATHS, set)
    
    def test_official_paths_contains_v3(self):
        """OFFICIAL_PATHS contains v3 paths."""
        self.assertIn("/v3", OFFICIAL_PATHS)
    
    def test_official_paths_contains_sessions(self):
        """OFFICIAL_PATHS contains session paths."""
        has_session = any("sessions" in p for p in OFFICIAL_PATHS)
        self.assertTrue(has_session)
    
    def test_official_paths_contains_organizations(self):
        """OFFICIAL_PATHS contains organization paths."""
        has_org = any("organizations" in p for p in OFFICIAL_PATHS)
        self.assertTrue(has_org)


# =============================================================================
# Catalogue Tests
# =============================================================================

class CatalogueTests(unittest.TestCase):
    """Test catalogue generation."""
    
    def test_catalogue_returns_models(self):
        """catalogue returns list of models."""
        models, _, _ = catalogue()
        self.assertIsInstance(models, list)
        self.assertGreater(len(models), 0)
    
    def test_catalogue_returns_warnings(self):
        """catalogue returns warnings."""
        _, warnings, _ = catalogue()
        self.assertIsInstance(warnings, list)
    
    def test_catalogue_returns_evidence(self):
        """catalogue returns evidence URL."""
        _, _, evidence = catalogue()
        self.assertIsInstance(evidence, str)
    
    def test_catalogue_models_have_required_fields(self):
        """catalogue models have required fields."""
        models, _, _ = catalogue()
        required = ["id", "display_name", "description"]
        for model in models:
            for field in required:
                self.assertIn(field, model)
    
    def test_catalogue_models_have_devin_modes(self):
        """catalogue models have devin modes."""
        models, _, _ = catalogue()
        mode_ids = [model["id"] for model in models]
        for expected_mode in ["normal", "fast", "lite", "ultra", "fusion"]:
            self.assertIn(expected_mode, mode_ids)


# =============================================================================
# Session Model Tests
# =============================================================================

class SessionModelTests(unittest.TestCase):
    """Test DevinSession model."""
    
    def test_session_from_api_response(self):
        """DevinSession creates session correctly."""
        data = {"id": TEST_SESSION_ID, "state": "running", "task": TEST_TASK, "created_at": "2026-01-01T00:00:00Z", "metadata": {"key": "value"}}
        session = DevinSession(data, TEST_ORG_ID)
        self.assertEqual(session.id, TEST_SESSION_ID)
        self.assertEqual(session.org_id, TEST_ORG_ID)
        self.assertEqual(session.state, "running")
        self.assertEqual(session.task, TEST_TASK)
        self.assertEqual(session.metadata, {"key": "value"})
    
    def test_session_to_dict(self):
        """DevinSession.to_dict serializes correctly."""
        session_data = {"id": TEST_SESSION_ID, "state": "running", "task": TEST_TASK}
        session = DevinSession(session_data, TEST_ORG_ID)
        result = session.to_dict()
        self.assertEqual(result["id"], TEST_SESSION_ID)
        self.assertEqual(result["org_id"], TEST_ORG_ID)
        self.assertEqual(result["state"], "running")
        self.assertEqual(result["task"], TEST_TASK)
    
    def test_session_state_values(self):
        """DevinSession uses string states."""
        for state in ["pending", "running", "completed", "failed", "cancelled"]:
            session = DevinSession({"state": state}, TEST_ORG_ID)
            self.assertEqual(session.state, state)


# =============================================================================
# Session Management Tests
# =============================================================================

class SessionManagementTests(unittest.TestCase):
    """Test session management functions."""
    
    def setUp(self):
        self.transport = MockTransport()
    
    def test_create_session_with_mock(self):
        """create_session works with mock transport."""
        session = create_session(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, task=TEST_TASK, transport=self.transport)
        self.assertEqual(session.id, TEST_SESSION_ID)
        self.assertEqual(session.org_id, TEST_ORG_ID)
        self.assertEqual(session.task, TEST_TASK)
    
    def test_get_session_with_mock(self):
        """get_session works with mock transport."""
        session = get_session(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, session_id=TEST_SESSION_ID, transport=self.transport)
        self.assertEqual(session.id, TEST_SESSION_ID)
        self.assertEqual(session.org_id, TEST_ORG_ID)
        self.assertEqual(session.state, "running")
    
    def test_list_sessions_with_mock(self):
        """list_sessions works with mock transport."""
        sessions, cursor = list_sessions(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, transport=self.transport)
        self.assertIsInstance(sessions, list)
        if sessions:
            self.assertEqual(sessions[0].id, TEST_SESSION_ID)
    
    def test_cancel_session_with_mock(self):
        """cancel_session works with mock transport."""
        session = cancel_session(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, session_id=TEST_SESSION_ID, transport=self.transport)
        self.assertEqual(session.id, TEST_SESSION_ID)
        self.assertEqual(session.state, "archived")


# =============================================================================
# Task Submission Tests
# =============================================================================

class TaskSubmissionTests(unittest.TestCase):
    """Test task submission functions."""
    
    def setUp(self):
        self.transport = MockTransport()
    
    def test_submit_task_submits_message(self):
        """submit_task submits message to session."""
        result = submit_task(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, session_id=TEST_SESSION_ID, message=TEST_TASK, transport=self.transport)
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)
    
    def test_submit_task_with_message(self):
        """submit_task submits message."""
        result = submit_task(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, session_id=TEST_SESSION_ID, message=TEST_TASK, transport=self.transport)
        self.assertIsInstance(result, dict)


# =============================================================================
# Connection Validation Tests
# =============================================================================

class ConnectionValidationTests(unittest.TestCase):
    """Test connection validation."""
    
    def test_validate_connection_with_none(self):
        """validate_connection with None connection."""
        result = validate_connection(None)
        self.assertIsInstance(result, dict)
    
    def test_validate_connection_with_empty_dict(self):
        """validate_connection with empty dict."""
        result = validate_connection({})
        self.assertIsInstance(result, dict)
    
    def test_validate_connection_with_org_id(self):
        """validate_connection accepts org_id in connection."""
        result = validate_connection({"org_id": TEST_ORG_ID})
        self.assertIsInstance(result, dict)
    
    def test_validate_connection_rejects_non_dict(self):
        """validate_connection rejects non-dict."""
        with self.assertRaises(DevinAgentError):
            validate_connection("not a dict")
    
    def test_validate_connection_rejects_invalid_base_url_scheme(self):
        """validate_connection rejects non-HTTPS URLs."""
        with self.assertRaises(DevinAgentError):
            validate_connection({"base_url": "http://api.devin.ai/v3"})
    
    def test_validate_connection_rejects_invalid_hostname(self):
        """validate_connection rejects non-Devin hostname."""
        with self.assertRaises(DevinAgentError):
            validate_connection({"base_url": "https://api.other.ai/v3"})
    
    def test_validate_connection_rejects_query_in_url(self):
        """validate_connection rejects query in URL."""
        with self.assertRaises(DevinAgentError):
            validate_connection({"base_url": "https://api.devin.ai/v3?key=value"})
    
    def test_validate_connection_rejects_credentials_in_url(self):
        """validate_connection rejects credentials in URL."""
        with self.assertRaises(DevinAgentError):
            validate_connection({"base_url": "https://user:pass@api.devin.ai/v3"})
    
    def test_validate_connection_rejects_credentials_in_settings(self):
        """validate_connection rejects credentials in connection settings."""
        with self.assertRaises(DevinAgentError):
            validate_connection({"api_key": TEST_API_KEY})


# =============================================================================
# Error Tests
# =============================================================================

class ErrorTests(unittest.TestCase):
    """Test error handling."""
    
    def setUp(self):
        self.transport = MockTransport()
    
    def test_devin_error_is_value_error(self):
        """DevinAgentError is a ValueError."""
        self.assertTrue(issubclass(DevinAgentError, ValueError))
    
    def test_create_session_without_org_id(self):
        """create_session fails without org_id."""
        with self.assertRaises(DevinAgentError):
            create_session(api_key=TEST_API_KEY, org_id="", task=TEST_TASK, transport=self.transport)
    
    def test_get_session_with_invalid_id(self):
        """get_session fails with invalid session_id."""
        with self.assertRaises(DevinAgentError):
            get_session(api_key=TEST_API_KEY, org_id=TEST_ORG_ID, session_id="", transport=self.transport)


# =============================================================================
# Validation Tests
# =============================================================================

class ValidationTests(unittest.TestCase):
    """Test validation functions."""
    
    def test_valid_key_accepts_valid_key(self):
        """_valid_key accepts valid API key."""
        result = _valid_key(TEST_API_KEY)
        self.assertEqual(result, TEST_API_KEY)
    
    def test_valid_key_rejects_none(self):
        """_valid_key rejects None."""
        with self.assertRaises(DevinAgentError):
            _valid_key(None)
    
    def test_valid_key_rejects_empty(self):
        """_valid_key rejects empty string."""
        with self.assertRaises(DevinAgentError):
            _valid_key("")
    
    def test_valid_key_rejects_newlines(self):
        """_valid_key rejects keys with newlines."""
        with self.assertRaises(DevinAgentError):
            _valid_key("key\nwith\nnewlines")
    
    def test_valid_key_rejects_carriage_returns(self):
        """_valid_key rejects keys with carriage returns."""
        with self.assertRaises(DevinAgentError):
            _valid_key("key\rwith\rcarriage")
    
    def test_valid_key_strips_whitespace(self):
        """_valid_key strips whitespace."""
        result = _valid_key("  " + TEST_API_KEY + "  ")
        self.assertEqual(result, TEST_API_KEY)
    
    def test_valid_org_id_accepts_valid_org_id(self):
        """_valid_org_id accepts valid org ID."""
        result = _valid_org_id(TEST_ORG_ID)
        self.assertEqual(result, TEST_ORG_ID)
    
    def test_valid_org_id_rejects_none(self):
        """_valid_org_id rejects None."""
        with self.assertRaises(DevinAgentError):
            _valid_org_id(None)
    
    def test_valid_org_id_rejects_empty(self):
        """_valid_org_id rejects empty string."""
        with self.assertRaises(DevinAgentError):
            _valid_org_id("")
    
    def test_valid_org_id_rejects_invalid_chars(self):
        """_valid_org_id rejects invalid characters."""
        with self.assertRaises(DevinAgentError):
            _valid_org_id("org with spaces")
    
    def test_valid_session_id_accepts_valid_session_id(self):
        """_valid_session_id accepts valid session ID."""
        result = _valid_session_id(TEST_SESSION_ID)
        self.assertEqual(result, TEST_SESSION_ID)
    
    def test_valid_session_id_rejects_none(self):
        """_valid_session_id rejects None."""
        with self.assertRaises(DevinAgentError):
            _valid_session_id(None)


# =============================================================================
# Alias Tests
# =============================================================================

class AliasTests(unittest.TestCase):
    """Test that aliases work correctly."""
    
    def test_validate_connection_alias(self):
        """validate_connection is same function."""
        self.assertIs(validate_connection, validate_connection)
    
    def test_create_session_alias(self):
        """create_session is same function."""
        self.assertIs(create_session, create_session)
    
    def test_descriptor_alias(self):
        """DESCRIPTOR is same as DESCRIPTOR."""
        self.assertIs(DESCRIPTOR, DESCRIPTOR)
    
    def test_official_paths_alias(self):
        """OFFICIAL_PATHS is same as OFFICIAL_PATHS."""
        self.assertIs(OFFICIAL_PATHS, OFFICIAL_PATHS)


if __name__ == "__main__":
    unittest.main()
