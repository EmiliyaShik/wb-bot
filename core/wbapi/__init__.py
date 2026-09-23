"""Единственное место в проекте, которое знает про HTTP и токены WB.

Наружу выставлены get_wb_client(client_id), verify_token(raw) и probe_hosts().
Заголовки, хосты, версии методов, ограничитель частоты, ретраи и сам токен
остаются внутри. Агент, которому нужен WB, берёт клиент и вызывает обёртку;
строку токена он не получит ниоткуда.
"""

from __future__ import annotations

from core.wbapi.errors import (
    WBApiError,
    WBAuthError,
    WBError,
    WBForbiddenError,
    WBRateLimited,
    WBTokenFormatError,
    WBTokenMissing,
    WBUnavailable,
)
from core.wbapi.client import (
    ENDPOINTS,
    Endpoint,
    ReportPages,
    RetryPolicy,
    close_session,
    load_token,
    retry_policy,
    shared_session,
    HOST_CATEGORY,
    HOSTS,
    WBClient,
    get_wb_client,
)
from core.wbapi.diag import (
    HostProbe,
    TokenCheck,
    check_token,
    check_token_live,
    probe_hosts,
    verdict_for,
)
from core.wbapi.limits import Budget, Limit, reset_limits
from core.wbapi.token import (
    ACC_TITLES,
    CATEGORY_BITS,
    CATEGORY_TITLES,
    READ_ONLY_BIT,
    TokenInfo,
    categories_from_mask,
    category_title,
    verify_token,
)

__all__ = [
    "ACC_TITLES",
    "Budget",
    "ENDPOINTS",
    "Endpoint",
    "ReportPages",
    "RetryPolicy",
    "close_session",
    "load_token",
    "retry_policy",
    "shared_session",
    "HOSTS",
    "HOST_CATEGORY",
    "HostProbe",
    "Limit",
    "TokenCheck",
    "check_token",
    "check_token_live",
    "probe_hosts",
    "verdict_for",
    "WBClient",
    "get_wb_client",
    "reset_limits",
    "CATEGORY_BITS",
    "CATEGORY_TITLES",
    "READ_ONLY_BIT",
    "TokenInfo",
    "WBApiError",
    "WBAuthError",
    "WBError",
    "WBForbiddenError",
    "WBRateLimited",
    "WBTokenFormatError",
    "WBTokenMissing",
    "WBUnavailable",
    "categories_from_mask",
    "category_title",
    "verify_token",
]
