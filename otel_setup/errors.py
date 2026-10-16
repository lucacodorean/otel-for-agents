class SetupError(Exception):
    """A user-facing error: printed without a traceback."""


class ConfigError(SetupError):
    """Invalid or missing configuration values."""


class MarketplaceError(SetupError):
    """Marketplace fetch, catalog parsing or allowlist derivation failed."""
