"""User-creation helpers shared between manual API + sync orchestrators."""

from .. import db
from ..qr_utils import create_token, generate_qr_png


def issue_initial_token(user_id: int,
                         comment: str = 'auto-created with user') -> str:
    """Generate one QR token for a user + render the PNG. Used both by
    the manual user-create route and by external-source sync (Buildium,
    etc.) so every new user is immediately scan-ready and we don't
    duplicate the token-creation/PNG dance in two places."""
    token = create_token()
    db.create_token_for_user(user_id, token, comment=comment)
    short_id = db.get_token_short_id(token)
    generate_qr_png(token, payload=short_id)
    return token
