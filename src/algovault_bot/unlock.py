"""TG-BROADCAST-STACK-W1 CH4 (2026-05-28): /unlock_premium_alerts state machine.

Viral acquisition mechanic — subscribers earn 30 days Pro by EITHER:
- X-follow path: tap [Follow X] inline button → state ``pending_x_screenshot``
  → upload screenshot → operator review (C5) → state ``verified`` + grant.
- npm-install path: tap [Install] inline button → state ``pending_npm_call``
  → user adds ``--track-token=<UUID>`` to their MCP config → MCP server
  emits funnel_events row on first tool call → cron detects (C6) →
  state ``verified`` + grant.

This module ships the SHELL: command handler + button bodies + state
transitions + funnel-event emits. Verifiers C5 + C6 close the loop.

Trilingual surface: en (default) + id (hellorekt) + zh-hans (falo08).
Other lang_codes fall back to en.

Spec reference: ``Prompt/tg-broadcast-stack-w1.md`` Chapter C4.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Final

log = logging.getLogger(__name__)

# State machine enum (subscribers.unlock_status TEXT column).
STATE_NOT_STARTED: Final = "not_started"
STATE_PENDING_X: Final = "pending_x_screenshot"
STATE_PENDING_NPM: Final = "pending_npm_call"
STATE_VERIFIED: Final = "verified"
STATE_EXPIRED: Final = "expired"

VALID_STATES = frozenset(
    {STATE_NOT_STARTED, STATE_PENDING_X, STATE_PENDING_NPM, STATE_VERIFIED, STATE_EXPIRED}
)

# Method enum (subscribers.unlock_method + tg_pro_grants.method).
METHOD_X_FOLLOW: Final = "x_follow"
METHOD_NPM_INSTALL: Final = "npm_install"
VALID_METHODS = frozenset({METHOD_X_FOLLOW, METHOD_NPM_INSTALL})

# Grant duration: 30 days per spec.
GRANT_DURATION_DAYS: Final = 30

# Pending-NPM expiry: 24h per spec ("I'll auto-detect within 24h").
PENDING_NPM_EXPIRY_HOURS: Final = 24
PENDING_X_EXPIRY_HOURS: Final = 24

# Callback data prefixes (Telegram CallbackQuery payload).
CB_UNLOCK_X: Final = "unlock:x"
CB_UNLOCK_NPM: Final = "unlock:npm"
CB_APPROVE_PREFIX: Final = "unlock_approve:"  # appended with chat_id
CB_REJECT_PREFIX: Final = "unlock_reject:"   # appended with chat_id


def normalize_lang(lang_code: str | None) -> str:
    """Trilingual routing: returns 'id' / 'zh-hans' / 'en' fallback."""
    if not lang_code:
        return "en"
    lc = lang_code.lower().replace("_", "-")
    if lc.startswith("id"):
        return "id"
    if lc.startswith("zh"):
        return "zh-hans"
    return "en"


def generate_track_token() -> str:
    """UUIDv4 hex (no dashes) for embedding in npx mcpServers config arg."""
    return uuid.uuid4().hex


def compute_grant_expiry(now: datetime | None = None) -> datetime:
    """Return the 30-day-from-now expiry timestamp."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now + timedelta(days=GRANT_DURATION_DAYS)


def is_pending_x_expired(pending_since: datetime | None, now: datetime | None = None) -> bool:
    if pending_since is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - pending_since) >= timedelta(hours=PENDING_X_EXPIRY_HOURS)


def is_pending_npm_expired(pending_since: datetime | None, now: datetime | None = None) -> bool:
    if pending_since is None:
        return False
    now = now or datetime.now(timezone.utc)
    return (now - pending_since) >= timedelta(hours=PENDING_NPM_EXPIRY_HOURS)


# ── Trilingual bodies ───────────────────────────────────────────────────


def format_intro_body(lang_code: str | None = None) -> str:
    """Initial /unlock_premium_alerts reply — 2-button keyboard intro.
    Body is the message text; the 2 buttons render via InlineKeyboardMarkup
    in handlers.py.
    """
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            "Dapatkan 30 hari Pro GRATIS dengan salah satu cara:\n\n"
            "🐦 Follow @AlgoVaultLabs di X + kirim screenshot\n"
            "📦 Install AlgoVault MCP di Claude Code / Cursor + buat 1 panggilan\n\n"
            "Pilih jalur di bawah:"
        )
    if lang == "zh-hans":
        return (
            "通过任一方式免费获得 30 天 Pro：\n\n"
            "🐦 在 X 上关注 @AlgoVaultLabs + 发送截图\n"
            "📦 在 Claude Code / Cursor 中安装 AlgoVault MCP + 进行 1 次调用\n\n"
            "在下方选择路径："
        )
    return (
        "Earn 30 days Pro FREE one of two ways:\n\n"
        "🐦 Follow @AlgoVaultLabs on X + send screenshot\n"
        "📦 Install AlgoVault MCP on Claude Code / Cursor + make 1 call\n\n"
        "Pick a path below:"
    )


def format_button_labels(lang_code: str | None = None) -> tuple[str, str]:
    """Returns (x_follow_label, npm_install_label) for the InlineKeyboardMarkup."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return ("🐦 Follow @AlgoVaultLabs di X", "📦 Install di Claude / Cursor")
    if lang == "zh-hans":
        return ("🐦 在 X 上关注 @AlgoVaultLabs", "📦 在 Claude / Cursor 中安装")
    return ("🐦 Follow @AlgoVaultLabs on X", "📦 Install on Claude / Cursor")


def format_pending_x_body(lang_code: str | None = None) -> str:
    """Reply after [Follow X] tap — instructions to send screenshot."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            "Follow @AlgoVaultLabs di X (https://x.com/AlgoVaultLabs), "
            "lalu kirim screenshot yang menunjukkan follow tersebut. "
            "Saya akan verifikasi dalam 24 jam dan memberikan 30 hari Pro."
        )
    if lang == "zh-hans":
        return (
            "在 X 上关注 @AlgoVaultLabs（https://x.com/AlgoVaultLabs），"
            "然后发送显示关注的截图。"
            "我将在 24 小时内验证并授予 30 天 Pro。"
        )
    return (
        "Follow @AlgoVaultLabs on X (https://x.com/AlgoVaultLabs), "
        "then send me a screenshot showing the follow. "
        "I'll verify within 24h and grant 30 days Pro."
    )


def format_pending_npm_body(track_token: str, lang_code: str | None = None) -> str:
    """Reply after [Install] tap — mcpServers config snippet + instructions."""
    lang = normalize_lang(lang_code)
    snippet = (
        '{"mcpServers":{"algovault":{"command":"npx",'
        f'"args":["crypto-quant-signal-mcp","--track-token={track_token}"]}}}}'
    )
    if lang == "id":
        return (
            "Salin ini ke konfigurasi MCP Claude Code / Cursor:\n\n"
            f"```\n{snippet}\n```\n\n"
            "Lalu buat panggilan verdict apa saja (mis. 'minta verdict BTC'). "
            "Saya akan auto-detect dalam 24 jam dan memberikan 30 hari Pro."
        )
    if lang == "zh-hans":
        return (
            "将此复制到您的 Claude Code / Cursor MCP 配置中：\n\n"
            f"```\n{snippet}\n```\n\n"
            "然后进行任何 verdict 调用（例如 '帮我查 BTC 的 verdict'）。"
            "我将在 24 小时内自动检测并授予 30 天 Pro。"
        )
    return (
        "Copy this into your Claude Code / Cursor MCP config:\n\n"
        f"```\n{snippet}\n```\n\n"
        "Then make any verdict call (e.g. 'get me a BTC verdict'). "
        "I'll auto-detect within 24h and grant 30 days Pro."
    )


def format_verified_body(method: str, lang_code: str | None = None) -> str:
    """Reply after operator [Approve] or auto-detection — Pro granted."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        if method == METHOD_X_FOLLOW:
            return "Terverifikasi! 30 hari Pro dimulai sekarang. Gunakan /verdict dengan bebas."
        return "Terverifikasi! Panggilan pertama terdeteksi. 30 hari Pro dimulai sekarang."
    if lang == "zh-hans":
        if method == METHOD_X_FOLLOW:
            return "已验证！30 天 Pro 现在开始。随意使用 /verdict。"
        return "已验证！检测到首次调用。30 天 Pro 现在开始。"
    if method == METHOD_X_FOLLOW:
        return "Verified! 30 days Pro starts now. Use /verdict freely."
    return "Verified! First call detected. 30 days Pro starts now."


def format_rejected_body(lang_code: str | None = None) -> str:
    """Reply after operator [Reject] — request a clearer screenshot."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            "Screenshot tidak dapat diverifikasi. Coba /unlock_premium_alerts lagi "
            "dengan screenshot yang lebih jelas menunjukkan follow @AlgoVaultLabs."
        )
    if lang == "zh-hans":
        return (
            "无法验证截图。请使用 /unlock_premium_alerts 再次尝试，"
            "提供清晰显示关注 @AlgoVaultLabs 的截图。"
        )
    return (
        "Screenshot couldn't be verified. Try /unlock_premium_alerts again "
        "with a clearer screenshot showing the @AlgoVaultLabs follow."
    )


def format_expired_body(lang_code: str | None = None) -> str:
    """Reply when 24h passes without npm-install detection or X screenshot."""
    lang = normalize_lang(lang_code)
    if lang == "id":
        return (
            "24 jam berlalu tanpa verifikasi terdeteksi. "
            "Coba /unlock_premium_alerts lagi untuk mulai ulang."
        )
    if lang == "zh-hans":
        return (
            "24 小时已过，未检测到验证。"
            "请使用 /unlock_premium_alerts 再次尝试重新开始。"
        )
    return (
        "24h passed without verification. "
        "Try /unlock_premium_alerts again to restart."
    )


def format_already_verified_body(expires_at: datetime, lang_code: str | None = None) -> str:
    """Reply when /unlock fires on a subscriber who already has an active grant."""
    lang = normalize_lang(lang_code)
    expires_str = expires_at.strftime("%Y-%m-%d")
    if lang == "id":
        return f"Anda sudah memiliki Pro aktif hingga {expires_str}. Tidak perlu /unlock_premium_alerts lagi."
    if lang == "zh-hans":
        return f"您已经拥有 Pro，有效期至 {expires_str}。无需再次使用 /unlock_premium_alerts。"
    return f"You already have active Pro until {expires_str}. No need to /unlock_premium_alerts again."
