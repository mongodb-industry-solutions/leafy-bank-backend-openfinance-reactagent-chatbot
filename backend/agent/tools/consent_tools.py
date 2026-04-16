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
    "REPAYMENT_HISTORY_READ",
    "CUSTOMER_IDENTIFICATION_READ",
    "TRANSACTIONS_READ",
]

# Default permissions per consent purpose
PURPOSE_PERMISSIONS = {
    "PERSONAL_LOAN_PORTABILITY": ALL_PERMISSIONS.copy(),
    "PAYROLL_LOAN_PORTABILITY": ALL_PERMISSIONS.copy(),
    "VEHICLE_LOAN_PORTABILITY": ALL_PERMISSIONS.copy(),
    "FINANCIAL_ADVICE": [
        "ACCOUNTS_READ",
        "ACCOUNTS_BALANCES_READ",
        "TRANSACTIONS_READ",
    ],
}

# User-facing benefit descriptions per permission — the agent presents these
# directly from tool output instead of recalling from prompt memory.
_PERMISSION_BENEFITS = {
    "LOANS_READ": "Loan details — current rate, outstanding balance, remaining term. Feeds the portability calculation to guarantee exact savings before you commit.",
    "ACCOUNTS_READ": "Account info — account ownership and banking relationship. Confirms eligibility and strengthens the application.",
    "ACCOUNTS_BALANCES_READ": "Balances — current balances across accounts. Feeds debt-to-income ratio, may unlock better rate tiers.",
    "REPAYMENT_HISTORY_READ": "Repayment history — payment track record over recent months. Demonstrates reliability, helps qualify for premium rates.",
    "CUSTOMER_IDENTIFICATION_READ": "Identity verification — one-time regulatory check required by the Central Bank. Never stored after verification.",
    "TRANSACTIONS_READ": "Transaction history — income deposits and spending patterns. Builds credit profile — typically reduces rates by 0.5-2.5% (est. R$1,200-3,600/year depending on loan size).",
}

_FINANCIAL_ADVICE_BENEFITS = {
    "ACCOUNTS_READ": "Account info — complete overview, helps spot optimization opportunities.",
    "ACCOUNTS_BALANCES_READ": "Balances — full financial picture across all banks.",
    "TRANSACTIONS_READ": "Transaction history — identifies where you're overspending vs doing well.",
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
        purpose: The consent purpose. One of: PERSONAL_LOAN_PORTABILITY, PAYROLL_LOAN_PORTABILITY, VEHICLE_LOAN_PORTABILITY, FINANCIAL_ADVICE. Omit or pass null for general access (all permissions).
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
        purpose: Consent purpose. One of: PERSONAL_LOAN_PORTABILITY, PAYROLL_LOAN_PORTABILITY, VEHICLE_LOAN_PORTABILITY, FINANCIAL_ADVICE. Omit for general access (all permissions).
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        body = {
            "consumer_id": user_id,
            "source_institution_name": source_institution_name,
            "expiration_days": 30,
            "permissions": permissions,
        }
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
async def verify_consent_data(consent_id: str, config: RunnableConfig) -> str:
    """Verify what data is accessible after consent approval. Returns a summary with
    counts and key values for each data category (accounts, loans, transactions,
    repayment history, customer identification).

    Use this after a consent is approved to confirm data access and show the user
    exactly what was received vs what was not.

    WARNING: For one-time consents (duration = 0), calling this will consume the
    consent. Only use with duration-based consents (duration > 0).

    Args:
        consent_id: The approved consent ID to verify data access for
    """
    user_id = config["configurable"]["user_id"]
    profile = config["configurable"].get("profile")
    try:
        token = await get_bearer_token(user_id)
        params = {"consent_id": consent_id}
        if profile:
            params["profile"] = profile
        response = await http_client.get(
            f"/openfinance/secure/customers/{user_id}/external-data",
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
                        "currency": a.get("Currency", "BRL"),
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

        # Repayment history
        repayment = data.get("repayment_history")
        if repayment:
            summary["data_received"]["repayment_history"] = {
                "received": True,
                "count": len(repayment),
            }
        else:
            summary["data_received"]["repayment_history"] = {"received": False}

        # Customer identification (KYC)
        kyc = data.get("customer_identification")
        if kyc:
            summary["data_received"]["customer_identification"] = {
                "received": True,
            }
        else:
            summary["data_received"]["customer_identification"] = {"received": False}

        # Build not_received list
        not_received = [
            category
            for category, info in summary["data_received"].items()
            if not info.get("received")
        ]
        if not_received:
            summary["not_received"] = not_received

        return json.dumps(summary)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error verifying consent data: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error verifying consent data (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error verifying consent data: {e}")
        return f"Error verifying consent data: {str(e)}"
