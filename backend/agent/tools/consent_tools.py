import json
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.types import interrupt

from http_client import http_client
from agent.tools.auth import get_bearer_token

logger = logging.getLogger(__name__)

# All available permissions (superset) — used for general access (no purpose)
ALL_PERMISSIONS = [
    "LOANS_READ",
    "ACCOUNTS_READ",
    "ACCOUNTS_BALANCES_READ",
    "TRANSACTIONS_READ",
]

# Foundational permission. In Open Finance, balances, transactions, and products
# all belong to an account — they cannot be shared without account info. So
# ACCOUNTS_READ is mandatory on every consent and can never be removed.
MANDATORY_PERMISSION = "ACCOUNTS_READ"

# Default permissions per consent purpose
PURPOSE_PERMISSIONS = {
    "FINANCIAL_ADVICE": [
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "TRANSACTIONS_READ",
        "LOANS_READ",
    ],
}

# User-facing benefit descriptions per permission — the agent presents these
# directly from tool output instead of recalling from prompt memory.
_PERMISSION_BENEFITS = {
    "LOANS_READ": "Product details — loans and credit products, current rates, outstanding balances, remaining terms. Gives a complete picture of your obligations across banks.",
    "ACCOUNTS_READ": "Account info — account ownership and banking relationship. Confirms eligibility and completes your financial overview.",
    "ACCOUNTS_BALANCES_READ": "Balances — current balances across accounts. Feeds debt-to-income ratio and net-worth calculations.",
    "TRANSACTIONS_READ": "Transaction history — income deposits and spending patterns. Powers spending insights and personalized financial advice.",
}

_FINANCIAL_ADVICE_BENEFITS = {
    "ACCOUNTS_READ": "Account info — complete overview, helps spot optimization opportunities.",
    "ACCOUNTS_BALANCES_READ": "Balances — full financial picture across all banks.",
    "TRANSACTIONS_READ": "Transaction history — identifies where you're overspending vs doing well.",
    "LOANS_READ": "Loans & credit products — current rates, balances, and terms so advice reflects your full debt picture.",
}


@tool
async def list_institutions() -> str:
    """List all authorized external banking institutions available for data sharing.
    Present results using the exact wording returned (e.g. 'Open Finance authorized institutions are: ...')."""
    try:
        response = await http_client.get("/openfinance/secure/institutions/")
        response.raise_for_status()
        data = response.json()
        institutions = data.get("institutions", [])
        names = [inst.get("InstitutionName", "Unknown") for inst in institutions]
        return f"Open Finance authorized institutions are: {', '.join(names)}"
    except Exception as e:
        logger.error(f"Error listing institutions: {e}")
        return f"Error fetching institutions: {str(e)}"


@tool
async def get_default_permissions(purpose: Optional[str] = None) -> str:
    """Get the default data permissions and their user-facing benefit descriptions for a given consent purpose.
    ALWAYS call this before explaining scope to the user. Present ALL listed permissions — do not omit any.
    The returned descriptions are written for the user — present them directly.

    Args:
        purpose: The consent purpose. Currently only FINANCIAL_ADVICE is supported. Omit or pass null for general access (all permissions).
    """
    if purpose is None:
        permissions = ALL_PERMISSIONS
        lines = [f"  - {_PERMISSION_BENEFITS.get(p, p)}" for p in permissions]
        return "Default permissions for general access (all data):\n" + "\n".join(lines)

    purpose = purpose.upper()
    if purpose not in PURPOSE_PERMISSIONS:
        return f"Unknown purpose '{purpose}'. Valid purposes: {', '.join(PURPOSE_PERMISSIONS.keys())} (or omit for general access)"

    permissions = PURPOSE_PERMISSIONS[purpose]
    is_advice = purpose == "FINANCIAL_ADVICE"
    benefit_map = _FINANCIAL_ADVICE_BENEFITS if is_advice else _PERMISSION_BENEFITS
    lines = [f"  - {benefit_map.get(p, _PERMISSION_BENEFITS.get(p, p))}" for p in permissions]
    return f"Default permissions for {purpose}:\n" + "\n".join(lines)


@tool
async def create_consent(
    source_institution_name: str,
    permissions: list[str],
    config: RunnableConfig,
    purpose: Optional[str] = None,
) -> str:
    """Create a new data sharing consent. Only call this AFTER the user has reviewed and confirmed the scope.
    Duration is fixed at 30 days — do not ask the user about duration.

    Args:
        source_institution_name: Name of the external bank to connect to
        permissions: List of approved permissions (e.g. ["LOANS_READ", "ACCOUNTS_READ"])
        purpose: Consent purpose. Currently only FINANCIAL_ADVICE is supported. Omit for general access (all permissions).
    """
    # ACCOUNTS_READ is foundational — balances, transactions, and products all
    # belong to an account and can't be shared without it. Silently re-inject it
    # if the reduced list dropped it, so an invalid consent can never be created.
    if MANDATORY_PERMISSION not in permissions:
        logger.info(
            "Re-injecting mandatory %s into consent permissions (was: %s)",
            MANDATORY_PERMISSION, permissions,
        )
        permissions = [MANDATORY_PERMISSION, *permissions]

    user_id = config["configurable"]["user_id"]
    # Scope duplicate detection to this conversation (browser session) so a new
    # session can create its own consent for a bank without colliding with an
    # active consent from another session.
    session_id = config["configurable"].get("thread_id")
    try:
        token = await get_bearer_token(user_id)
        body = {
            "consumer_id": user_id,
            "source_institution_name": source_institution_name,
            "expiration_days": 30,
            "permissions": permissions,
        }
        if session_id is not None:
            body["session_id"] = session_id
        if purpose is not None:
            body["purpose"] = purpose.upper()
        response = await http_client.post(
            "/openfinance/secure/consents/",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "type": consent.get("ConsentType"),
            "permissions": consent.get("Permissions"),
            "purpose": consent.get("Purpose"),
            "source_institution": consent.get("SourceInstitution", {}).get("InstitutionName"),
            "expiration": consent.get("ExpirationDateTime"),
            "display_expiration": consent.get("DisplayExpirationDateTime"),
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error creating consent: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error creating consent (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error creating consent: {e}")
        return f"Error creating consent: {str(e)}"


@tool
async def get_consent(consent_id: str, config: RunnableConfig) -> str:
    """Get the current details and status of a specific consent.

    Args:
        consent_id: The consent ID (URN format)
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            f"/openfinance/secure/consents/{consent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "type": consent.get("ConsentType"),
            "permissions": consent.get("Permissions"),
            "purpose": consent.get("Purpose"),
            "source_institution": consent.get("SourceInstitution", {}).get("InstitutionName"),
            "expiration": consent.get("ExpirationDateTime"),
            "display_expiration": consent.get("DisplayExpirationDateTime"),
            "created": consent.get("CreationDateTime"),
        })
    except Exception as e:
        logger.error(f"Error fetching consent: {e}")
        return f"Error fetching consent: {str(e)}"


@tool
async def list_user_consents(config: RunnableConfig) -> str:
    """List active (AUTHORISED) consents for the current user.

    Returns only AUTHORISED consents — expired, consumed, and pending consents
    are excluded. These are historical consents across all sessions, not just
    the current conversation. Use the supervisor's active_consents handoff to
    determine which consents belong to THIS session.
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            "/openfinance/secure/consents/",
            params={"consumer_id": user_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consents = data.get("consents", [])

        # Only return AUTHORISED consents — EXPIRED/CONSUMED/AWAITING are noise
        results = []
        for c in consents:
            if (c.get("Status") or "").upper() != "AUTHORISED":
                continue
            results.append({
                "consent_id": c.get("ConsentId"),
                "status": c.get("Status"),
                "purpose": c.get("Purpose"),
                "source": c.get("SourceInstitution", {}).get("InstitutionName"),
                "type": c.get("ConsentType"),
                "expiration": c.get("ExpirationDateTime"),
                "display_expiration": c.get("DisplayExpirationDateTime"),
            })

        if not results:
            return "No active consents found for this user."
        return json.dumps(results)
    except Exception as e:
        logger.error(f"Error listing consents: {e}")
        return f"Error listing consents: {str(e)}"


@tool
async def request_bank_login(
    consent_id: str, institution_name: str, config: RunnableConfig
) -> str:
    """Request the user to log in to their external bank. This will pause the conversation and redirect the user to the bank's login page.

    Args:
        consent_id: The consent ID that requires bank authentication
        institution_name: The name of the bank the user needs to log in to
    """
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.get(
            "/openfinance/secure/institutions/",
            params={"user_id": user_id},
        )
        response.raise_for_status()
        data = response.json()
        user_banks = [
            inst.get("InstitutionName", "")
            for inst in data.get("institutions", [])
        ]
        if institution_name not in user_banks:
            return (
                f"Login failed: {user_id} does not have an account at "
                f"{institution_name}. Available banks with accounts: "
                f"{', '.join(user_banks) if user_banks else 'none'}. "
                f"Suggest the user connect to one of these banks instead."
            )
    except Exception as e:
        logger.warning(f"Could not verify user accounts at {institution_name}: {e}")
        # Proceed with login if the check fails — don't block on a validation error

    login_result = interrupt({
        "type": "BANK_LOGIN",
        "consent_id": consent_id,
        "institution_name": institution_name,
    })
    return f"Bank login completed. Result: {json.dumps(login_result)}"


@tool
async def approve_consent(consent_id: str, config: RunnableConfig) -> str:
    """Approve a consent that is in AWAITING_AUTHORISATION status. This will pause the conversation and ask the user for explicit approval before proceeding.

    Args:
        consent_id: The consent ID to approve
    """
    # Fetch consent details so the interrupt shows what the user is approving
    user_id = config["configurable"]["user_id"]
    interrupt_payload = {
        "type": "CONSENT_APPROVAL",
        "consent_id": consent_id,
        "message": "Please review and confirm that you approve this data-sharing consent.",
    }
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            f"/openfinance/secure/consents/{consent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        interrupt_payload["permissions"] = consent.get("Permissions", [])
        interrupt_payload["purpose"] = consent.get("Purpose")
        interrupt_payload["source_institution"] = (
            consent.get("SourceInstitution", {}).get("InstitutionName")
        )
        interrupt_payload["expiration"] = consent.get("ExpirationDateTime")
        interrupt_payload["display_expiration"] = consent.get("DisplayExpirationDateTime")
    except Exception as e:
        logger.warning(f"Could not fetch consent details for interrupt: {e}")

    # Hard interrupt — requires explicit human approval before consent is granted
    approval = interrupt(interrupt_payload)

    if not approval.get("approved"):
        return json.dumps({
            "consent_id": consent_id,
            "status": "AWAITING_AUTHORISATION",
            "message": "User declined consent approval. Consent was NOT approved.",
        })

    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.post(
            f"/openfinance/secure/consents/{consent_id}/approve",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "purpose": consent.get("Purpose"),
            "source_institution": consent.get("SourceInstitution", {}).get("InstitutionName"),
            "message": "Consent approved successfully.",
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error approving consent: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error approving consent (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error approving consent: {e}")
        return f"Error approving consent: {str(e)}"


@tool
async def revoke_consent(consent_id: str, config: RunnableConfig) -> str:
    """Revoke an active consent. This will immediately stop data access from the external bank.

    Args:
        consent_id: The consent ID to revoke
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.delete(
            f"/openfinance/secure/consents/{consent_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()
        consent = data.get("consent", data)
        return json.dumps({
            "consent_id": consent.get("ConsentId"),
            "status": consent.get("Status"),
            "message": "Consent revoked successfully.",
        })
    except httpx.HTTPStatusError as e:
        logger.error(f"Error revoking consent: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error revoking consent (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error revoking consent: {e}")
        return f"Error revoking consent: {str(e)}"


@tool
async def fetch_and_cache_data(consent_id: str, config: RunnableConfig) -> str:
    """Pull the consent-permitted data from the connected bank and cache it in Leafy Bank.

    Call this once, immediately after a consent is approved. It fetches accounts,
    products, and transactions (gated by the consent's permissions), caches them so
    later financial-advice queries read from the cache without re-consuming the
    consent, and returns a summary of what was received (with per-category counts and
    key values) plus `cached_counts` per resource type.

    The consent must be AUTHORISED and DURATION_BASED (all consents created here are
    30-day duration-based, so this always applies).

    Args:
        consent_id: The approved consent ID to fetch and cache data for
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        params = {"consent_id": consent_id}
        response = await http_client.post(
            f"/openfinance/secure/customers/{user_id}/fetch-and-cache",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        data = response.json()

        summary = {"consent_id": consent_id, "data_received": {}}

        # Accounts
        accounts = data.get("accounts")
        if accounts:
            summary["data_received"]["accounts"] = {
                "received": True,
                "count": len(accounts),
                "details": [
                    {
                        "type": a.get("AccountType", "unknown"),
                        "sub_type": a.get("AccountSubType"),
                        "balance": a.get("AccountBalance"),
                        "currency": a.get("AccountCurrency", "USD"),
                    }
                    for a in accounts
                ],
            }
        else:
            summary["data_received"]["accounts"] = {"received": False}

        # Products (loans)
        products = data.get("products")
        if products:
            summary["data_received"]["products"] = {
                "received": True,
                "count": len(products),
                "details": [
                    {
                        "type": p.get("ProductType", "unknown"),
                        "sub_type": p.get("LoanSubType") or p.get("ProductSubType"),
                        "name": p.get("ProductName"),
                        "outstanding_balance": p.get("ProductOutstandingBalance")
                        or p.get("OutstandingAmount"),
                        "interest_rate": p.get("ProductInterestRate")
                        or p.get("InterestRate"),
                    }
                    for p in products
                ],
            }
        else:
            summary["data_received"]["products"] = {"received": False}

        # Transactions (ISO 20022-aligned format)
        transactions = data.get("transactions")
        if transactions:
            dates = []
            debit_count = 0
            credit_count = 0
            for t in transactions:
                txn_date = t.get("BookgDt")
                if txn_date:
                    dates.append(str(txn_date))
                cdt_dbt = t.get("CdtDbtInd", "")
                if cdt_dbt == "DBIT":
                    debit_count += 1
                elif cdt_dbt == "CRDT":
                    credit_count += 1
            dates.sort()
            txn_info = {
                "received": True,
                "count": len(transactions),
                "debit_count": debit_count,
                "credit_count": credit_count,
            }
            if dates:
                txn_info["date_range"] = {
                    "earliest": dates[0],
                    "latest": dates[-1],
                }
            summary["data_received"]["transactions"] = txn_info
        else:
            summary["data_received"]["transactions"] = {"received": False}

        # Build not_received list
        not_received = [
            category
            for category, info in summary["data_received"].items()
            if not info.get("received")
        ]
        if not_received:
            summary["not_received"] = not_received

        # Per-resource counts of what was cached (populated by fetch-and-cache)
        cached_counts = data.get("cached_counts")
        if cached_counts is not None:
            summary["cached_counts"] = cached_counts

        return json.dumps(summary)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error fetching and caching consent data: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error fetching and caching consent data (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error fetching and caching consent data: {e}")
        return f"Error fetching and caching consent data: {str(e)}"
