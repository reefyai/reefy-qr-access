"""Provider registry. Worker reads `cfg['provider']` and instantiates
the matching class with the saved config dict."""

from .base import (
    EmailError, EmailProvider,
    RateLimitError, TransientError, PermanentError,
)
from .smtp import SmtpProvider
from .agentmail import AgentMailProvider


PROVIDERS = {
    'smtp': SmtpProvider,
    'agentmail': AgentMailProvider,
}


def build_provider(cfg: dict, on_inbox_provisioned=None) -> EmailProvider:
    """Instantiate the right provider for this config. AgentMail
    needs a callback to persist the auto-provisioned inbox_id back
    into app_settings; other providers ignore extra kwargs."""
    name = (cfg.get('provider') or 'smtp').lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        raise ValueError(f'unknown email provider: {name!r}')
    if name == 'agentmail':
        return cls(cfg, on_inbox_provisioned=on_inbox_provisioned)
    return cls(cfg)


__all__ = [
    'EmailError', 'EmailProvider',
    'RateLimitError', 'TransientError', 'PermanentError',
    'SmtpProvider', 'AgentMailProvider',
    'PROVIDERS', 'build_provider',
]
