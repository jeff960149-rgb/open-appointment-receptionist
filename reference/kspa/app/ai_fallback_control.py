"""Persistent Kspa AI handoff switch, separate from human ring destinations."""
import json
import logging
from datetime import datetime, timezone

from app.storage import connect, _get_system_state, _set_system_state

KEY = 'kspa:ai_fallback_control:v1'
logger = logging.getLogger(__name__)


def get_state(default_enabled: bool) -> dict:
    with connect() as connection:
        raw = _get_system_state(connection, KEY)
    if raw is None:
        return {'enabled': bool(default_enabled), 'source': 'environment',
                'updated_at': None, 'updated_by': None}
    value = json.loads(raw)
    if not isinstance(value, dict) or type(value.get('enabled')) is not bool:
        raise ValueError('Invalid persisted AI handoff setting')
    return {**value, 'source': 'desktop_switch'}


def is_enabled(default_enabled: bool) -> bool:
    try:
        return get_state(default_enabled)['enabled']
    except Exception:
        # Human ringing remains available if the setting cannot be read.
        logger.exception('AI handoff setting unavailable; AI handoff stays off')
        return False


def set_enabled(enabled: bool, *, actor: str = 'desktop') -> dict:
    if type(enabled) is not bool:
        raise ValueError('AI handoff setting must be a boolean')
    value = {'enabled': enabled, 'updated_at': datetime.now(timezone.utc).isoformat(),
             'updated_by': actor}
    with connect() as connection:
        _set_system_state(connection, KEY, json.dumps(value))
    return {**value, 'source': 'desktop_switch'}
