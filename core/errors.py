class AutomationError(Exception):
    """Base class for errors raised by the automation adapters."""


class ConfigError(AutomationError):
    """Required configuration (.env, profile, selectors) is missing or invalid."""


class SessionExpiredError(AutomationError):
    """The saved browser session no longer authenticates; re-run the login flow."""


class IndexLoginError(AutomationError):
    """IndEX rejected the credentials. Never retried automatically (account lockout risk)."""


class LayoutChangedError(AutomationError):
    """The portal's page structure no longer matches the configured selectors."""
