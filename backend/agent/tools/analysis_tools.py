"""Tools for the analysis agent — financial data retrieval and evaluation."""

import asyncio
import json
import logging
from typing import Optional

import httpx
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from http_client import http_client
from agent.tools.auth import get_bearer_token, get_user_profile

logger = logging.getLogger(__name__)


# ---------- Tool 4: fetch_credit_score ----------


@tool
async def fetch_credit_score(config: RunnableConfig) -> str:
    """Fetch the user's credit bureau score. Used for the CreditBureau underwriting path (Personal loans > $1500)."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.get(
            f"/leafybank/customers/{user_id}/credit-score",
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching credit score: {e}")
        return f"Error fetching credit score: {str(e)}"


# ---------- Portability evaluation helpers ----------

# Maps consent purpose → expected loan sub-type for portability validation
_PURPOSE_TO_LOAN_TYPE = {
    "PERSONAL_LOAN_PORTABILITY": "Personal",
    "PAYROLL_LOAN_PORTABILITY": "PayrollDeductible",
    "VEHICLE_LOAN_PORTABILITY": "Vehicle",
}


def _find_applicable_rules(
    rules: list[dict],
    loan_sub_type: str,
    loan_amount: float,
    path: str,
) -> list[dict]:
    """Find underwriting rules matching loan sub-type, amount range, and path.

    Rules have overlapping ranges (e.g. baseline covers all amounts, spending-gt-1500
    covers >$1500). Returns matches sorted by specificity — more specific rules first.
    """
    applicable = []
    for rule in rules:
        if loan_sub_type not in rule.get("LoanSubTypes", []):
            continue
        if rule.get("Path") != path:
            continue
        amount_min = rule.get("LoanAmountMin", 0) or 0
        amount_max = rule.get("LoanAmountMax")
        if loan_amount < amount_min:
            continue
        if amount_max is not None and loan_amount > amount_max:
            continue
        applicable.append(rule)

    # Prefer specific rules over baseline (non-null LoanAmountMax first)
    applicable.sort(key=lambda r: (
        r.get("RuleId") == "baseline",
        r.get("LoanAmountMax") if r.get("LoanAmountMax") is not None else float("inf"),
    ))
    return applicable


def _match_score_to_tier(
    score: int | float,
    tiers: list[dict],
) -> dict | None:
    """Match a score against tier thresholds. Returns the best qualifying tier.

    Tiers are sorted descending by MinScore — the first tier the score meets
    or exceeds is the best one (lowest RateMultiplier).
    """
    for tier in sorted(tiers, key=lambda t: t["MinScore"], reverse=True):
        if score >= tier["MinScore"]:
            return tier
    return None


def _compute_monthly_payment(
    principal: float,
    annual_rate_pct: float,
    term_months: int,
) -> float:
    """Standard amortization: M = P × [r(1+r)^n] / [(1+r)^n - 1]."""
    if annual_rate_pct <= 0 or term_months <= 0:
        return round(principal / max(term_months, 1), 2)
    r = annual_rate_pct / 100.0 / 12.0
    n = term_months
    payment = principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)
    return round(payment, 2)


# ---------- Tool 5: evaluate_portability_offer ----------


@tool
async def evaluate_portability_offer(
    spending_score: int,
    current_rate: float,
    loan_amount: float,
    loan_sub_type: str,
    consent_purpose: Optional[str] = None,
    remaining_term_months: Optional[int] = None,
    credit_score: Optional[int] = None,
) -> str:
    """Evaluate a loan portability offer. Fetches underwriting rules and matching
    Leafy Bank products, then computes the qualified rate, monthly payments, and
    savings deterministically. Returns pre-computed results.

    Call this AFTER analyze_spending (which provides spending_score, current_rate,
    loan_amount, loan_sub_type from external_data) and optionally fetch_credit_score.

    Args:
        spending_score: Spending score 0-100 from analyze_spending
        current_rate: Current loan interest rate (%) from external_data products
        loan_amount: Outstanding loan balance from external_data products
        loan_sub_type: Loan sub-type from external_data (Personal, PayrollDeductible, Vehicle)
        consent_purpose: The consent purpose (PERSONAL_LOAN_PORTABILITY, PAYROLL_LOAN_PORTABILITY, VEHICLE_LOAN_PORTABILITY). Optional — omit for general access consents; the tool infers the purpose from loan_sub_type.
        remaining_term_months: Remaining loan term in months from external_data products. When provided, monthly payment and savings calculations are included.
        credit_score: Credit bureau score from fetch_credit_score (only needed for Personal loans > $1500)
    """
    try:
        writer = get_stream_writer()
    except Exception:
        def writer(_): pass

    try:
        # --- Validate consent purpose vs loan sub-type ---
        # All portability purposes and general access grant ALL_PERMISSIONS, so any
        # of them can be used for any loan type. Only FINANCIAL_ADVICE is restricted
        # (no LOANS_READ). Purpose-to-loan-type mismatch is informational, not blocking.
        loan_type_mismatch = None
        if consent_purpose and "FINANCIAL_ADVICE" in consent_purpose.upper():
            loan_type_mismatch = {
                "consent_purpose": consent_purpose,
                "actual_loan_type": loan_sub_type,
                "message": (
                    "The consent purpose is FINANCIAL_ADVICE, which does not include "
                    "loan data permissions. A portability consent or general access "
                    "consent is needed for loan portability analysis."
                ),
            }

        # --- Fetch underwriting rules and matching products concurrently ---
        writer({"type": "progress", "message": "Evaluating your portability offer..."})

        params = {
            "product_type": "Loan",
            "current_rate": current_rate,
            "current_amount": loan_amount,
            "loan_sub_type": loan_sub_type,
        }

        rules_task = http_client.get("/leafybank/portability/underwriting-rules")
        products_task = http_client.get(
            "/leafybank/products/secure/match", params=params
        )

        rules_resp, products_resp = await asyncio.gather(
            rules_task, products_task, return_exceptions=True
        )

        if isinstance(rules_resp, Exception):
            logger.error(f"Error fetching underwriting rules: {rules_resp}")
            return f"Error fetching underwriting rules: {str(rules_resp)}"
        rules_resp.raise_for_status()
        all_rules = rules_resp.json().get("rules", [])

        if isinstance(products_resp, Exception):
            logger.error(f"Error fetching matching products: {products_resp}")
            return f"Error fetching matching products: {str(products_resp)}"
        products_resp.raise_for_status()
        matching_products = products_resp.json().get("matches", [])

        writer({"type": "progress", "message": "Computing rates and savings..."})

        # --- Determine best rate multiplier across applicable paths ---

        # Spending path (always applies)
        spending_rules = _find_applicable_rules(
            all_rules, loan_sub_type, loan_amount, "Spending"
        )
        spending_tier = None
        spending_multiplier = None
        spending_rule_id = None
        if spending_rules:
            best_spending_rule = spending_rules[0]
            spending_rule_id = best_spending_rule.get("RuleId")
            spending_tier = _match_score_to_tier(
                spending_score, best_spending_rule.get("Tiers", [])
            )
            if spending_tier:
                spending_multiplier = spending_tier["RateMultiplier"]

        # Credit bureau path (only when credit_score is provided)
        credit_tier = None
        credit_multiplier = None
        credit_rule_id = None
        if credit_score is not None:
            credit_rules = _find_applicable_rules(
                all_rules, loan_sub_type, loan_amount, "CreditBureau"
            )
            if credit_rules:
                best_credit_rule = credit_rules[0]
                credit_rule_id = best_credit_rule.get("RuleId")
                credit_tier = _match_score_to_tier(
                    credit_score, best_credit_rule.get("Tiers", [])
                )
                if credit_tier:
                    credit_multiplier = credit_tier["RateMultiplier"]

        # Pick best (lowest) multiplier
        multipliers = []
        if spending_multiplier is not None:
            multipliers.append(("spending", spending_multiplier))
        if credit_multiplier is not None:
            multipliers.append(("credit_bureau", credit_multiplier))

        if not multipliers:
            return json.dumps({
                "status": "not_eligible",
                "spending_score": spending_score,
                "credit_score": credit_score,
                "message": (
                    f"Spending score of {spending_score} does not meet the minimum "
                    f"threshold for any underwriting tier."
                ),
            })

        best_path, best_multiplier = min(multipliers, key=lambda x: x[1])

        # --- Build evaluation summary ---
        evaluation = {
            "spending_score": spending_score,
            "spending_rule_id": spending_rule_id,
            "spending_multiplier": spending_multiplier,
            "credit_score": credit_score,
            "credit_rule_id": credit_rule_id,
            "credit_multiplier": credit_multiplier,
            "best_multiplier": best_multiplier,
            "best_path": best_path,
        }

        # --- Current loan payment ---
        current_loan = {
            "rate": current_rate,
            "amount": loan_amount,
            "remaining_term_months": remaining_term_months,
        }
        if remaining_term_months and remaining_term_months > 0:
            current_loan["monthly_payment"] = _compute_monthly_payment(
                loan_amount, current_rate, remaining_term_months
            )

        # --- Build offers with pre-computed rates and savings ---
        offers = []
        for product in matching_products:
            base_rate = product.get("ProductInterestRate", 0)
            qualified_rate = round(current_rate * best_multiplier, 2)
            rate_vs_current = round(current_rate - qualified_rate, 2)

            offer = {
                "product_id": product.get("ProductId"),
                "product_name": product.get("ProductName"),
                "base_rate": base_rate,
                "current_rate": current_rate,
                "qualified_rate": qualified_rate,
                "rate_improvement_vs_current": rate_vs_current,
                "loan_range": (
                    f"${product.get('MinAmount', 0):,.0f} - "
                    f"${product.get('MaxAmount', 0):,.0f}"
                ),
                "term_range": (
                    f"{product.get('MinTerm', '')} - "
                    f"{product.get('MaxTerm', '')} months"
                ),
            }

            if remaining_term_months and remaining_term_months > 0:
                offer["monthly_payment"] = _compute_monthly_payment(
                    loan_amount, qualified_rate, remaining_term_months
                )
                if "monthly_payment" in current_loan:
                    offer["monthly_savings"] = round(
                        current_loan["monthly_payment"] - offer["monthly_payment"], 2
                    )
                    offer["total_savings_over_term"] = round(
                        offer["monthly_savings"] * remaining_term_months, 2
                    )

            offers.append(offer)

        offers.sort(key=lambda o: o["qualified_rate"])

        # --- Human-readable summary ---
        path_label = "spending" if best_path == "spending" else "credit bureau"
        parts = [
            f"Your spending score of {spending_score} qualifies for a "
            f"{best_multiplier}x multiplier (via {path_label} path).",
        ]
        if credit_score is not None and credit_multiplier is not None:
            parts.append(
                f" Your credit score of {credit_score} qualifies for a "
                f"{credit_multiplier}x credit bureau multiplier."
            )

        if offers:
            best = offers[0]
            parts.append(
                f" Top Leafy Bank offer: {best['product_name']} at "
                f"{best['qualified_rate']}% "
                f"(your current {current_rate}% × {best_multiplier} multiplier)."
            )
            if "total_savings_over_term" in best and remaining_term_months:
                parts.append(
                    f" Over {remaining_term_months} months you'd save "
                    f"${best['monthly_savings']:.2f}/month "
                    f"(${best['total_savings_over_term']:.2f} total)."
                )
            else:
                parts.append(
                    f" That's {best['rate_improvement_vs_current']}% lower "
                    f"than your current {current_rate}% rate."
                )
        else:
            parts.append(
                " No Leafy Bank products currently offer a lower base rate "
                "than your existing loan for this loan type."
            )

        result = {
            "status": "evaluated",
            "evaluation": evaluation,
            "current_loan": current_loan,
            "offers": offers,
            "summary": "".join(parts),
        }
        if loan_type_mismatch:
            result["loan_type_warning"] = loan_type_mismatch
        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error evaluating portability offer: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error evaluating portability offer (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error evaluating portability offer: {e}")
        return f"Error evaluating portability offer: {str(e)}"


# ---------- Tool 6: fetch_internal_accounts ----------


@tool
async def fetch_internal_accounts(config: RunnableConfig) -> str:
    """Fetch all Leafy Bank accounts for the user."""
    user_id = config["configurable"]["user_id"]
    try:
        response = await http_client.post(
            "/leafybank/accounts/secure/fetch-accounts-for-user",
            json={"user_identifier": user_id},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except Exception as e:
        logger.error(f"Error fetching internal accounts: {e}")
        return f"Error fetching internal accounts: {str(e)}"


# ---------- Tool 7: calculate_financial_position ----------


@tool
async def calculate_financial_position(
    consent_id: str,
    config: RunnableConfig,
    connected_external_accounts: Optional[list[str]] = None,
    connected_external_products: Optional[list[str]] = None,
) -> str:
    """Calculate the user's total balance and total debt in a single call. Returns both aggregated balance (across all internal and external accounts) and aggregated debt (across all internal and external products). Used for DTI ratio in portability evaluation and financial overview.

    Args:
        consent_id: Any active consent ID for auth validation. The actual data scope is determined by connected_external_accounts and connected_external_products lists.
        connected_external_accounts: List of external account IDs to include (from banks_analyzed[].accounts across all banks)
        connected_external_products: List of external product IDs to include (from banks_analyzed[].products across all banks)
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        headers = {"Authorization": f"Bearer {token}"}

        # Resolve ObjectId from username (cached — avoids redundant HTTP call)
        user = await get_user_profile(user_id)
        user_object_id = user["_id"]

        balance_task = http_client.post(
            "/openfinance/secure/calculate-total-balance-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_accounts": connected_external_accounts or [],
                "consent_id": consent_id,
            },
            headers=headers,
        )
        debt_task = http_client.post(
            "/openfinance/secure/calculate-total-debt-for-user/",
            json={
                "user_id": user_object_id,
                "connected_external_products": connected_external_products or [],
                "consent_id": consent_id,
            },
            headers=headers,
        )

        balance_resp, debt_resp = await asyncio.gather(
            balance_task, debt_task, return_exceptions=True
        )

        result = {}

        if isinstance(balance_resp, Exception):
            logger.error(f"Error calculating total balance: {balance_resp}")
            result["balance_error"] = str(balance_resp)
        else:
            balance_resp.raise_for_status()
            result["total_balance"] = balance_resp.json()

        if isinstance(debt_resp, Exception):
            logger.error(f"Error calculating total debt: {debt_resp}")
            result["debt_error"] = str(debt_resp)
        else:
            debt_resp.raise_for_status()
            result["total_debt"] = debt_resp.json()

        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error calculating financial position: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error calculating financial position (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error calculating financial position: {e}")
        return f"Error calculating financial position: {str(e)}"


# ---------- Tool 10: fetch_customer_identification ----------


@tool
async def fetch_customer_identification(
    consent_id: str, config: RunnableConfig
) -> str:
    """Fetch external customer identification (KYC) data. Required by underwriting rules for portability evaluation.

    Args:
        consent_id: The consent ID with CUSTOMER_IDENTIFICATION_READ permission
    """
    user_id = config["configurable"]["user_id"]
    try:
        token = await get_bearer_token(user_id)
        response = await http_client.get(
            f"/leafybank/customers/{user_id}/identification",
            params={"consent_id": consent_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        return json.dumps(response.json())
    except httpx.HTTPStatusError as e:
        logger.error(f"Error fetching customer identification: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error fetching customer identification (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error fetching customer identification: {e}")
        return f"Error fetching customer identification: {str(e)}"


# ---------- Tool 11: find_user ----------


@tool
async def find_user(config: RunnableConfig) -> str:
    """Look up the Leafy Bank internal user profile (name, email, etc.)."""
    user_id = config["configurable"]["user_id"]
    try:
        user = await get_user_profile(user_id)
        return json.dumps({"user": user})
    except Exception as e:
        logger.error(f"Error finding user: {e}")
        return f"Error finding user: {str(e)}"


# ---------- Tool: analyze_spending ----------


def _build_mcc_to_category(best_practices: list[dict]) -> dict[str, dict]:
    """Build a mapping from MCC code → category info."""
    mcc_map = {}
    for category in best_practices:
        for mcc in category.get("MCCCodes", []):
            mcc_map[mcc] = {
                "CategoryId": category["CategoryId"],
                "CategoryName": category["CategoryName"],
                "IdealPercentage": category["IdealPercentage"],
                "MinPercentage": category["MinPercentage"],
                "MaxPercentage": category["MaxPercentage"],
            }
    return mcc_map


def _categorize_transactions(
    transactions: list[dict], mcc_map: dict[str, dict]
) -> tuple[dict[str, float], float, list[dict]]:
    """Categorize transactions in ISO 20022 format by MCC code.

    Works for both internal (Leafy Bank) and external bank transactions
    since both now use the same ISO 20022–aligned document structure.

    Returns (category_totals, total_spending, uncategorized_list).
    """
    category_totals: dict[str, float] = {}
    uncategorized = []
    total_spending = 0.0

    for txn in transactions:
        amount = txn.get("Amt", {}).get("value", 0)
        direction = txn.get("CdtDbtInd", "")
        txn_type = txn.get("TxTp", "")
        is_internal = txn.get("IntrnlTxn", False)

        # Internal transfers to savings
        if txn_type == "AccountTransfer" and is_internal:
            category_totals["savings"] = category_totals.get("savings", 0) + amount
            total_spending += amount
            continue

        # Skip CREDIT (income)
        if direction != "DBIT":
            continue

        total_spending += amount

        # Categorize by MCC
        mcc = txn.get("BkTxCd", {}).get("Prtry", {}).get("Cd", "")
        if mcc and mcc in mcc_map:
            cat_id = mcc_map[mcc]["CategoryId"]
            category_totals[cat_id] = category_totals.get(cat_id, 0) + amount
        else:
            uncategorized.append({
                "description": txn.get("AddtlNtryInf", ""),
                "amount": amount,
                "merchant": txn.get("Cdtr", {}).get("Nm", ""),
                "mcc": mcc,
                "bank": txn.get("Acct", {}).get("Svcr", ""),
            })

    return category_totals, total_spending, uncategorized


def _calculate_score(
    category_totals: dict[str, float],
    total_spending: float,
    best_practices: list[dict],
) -> tuple[int, list[dict]]:
    """Calculate a 0-100 spending score and per-category breakdown.

    Scoring: For each category, if actual % is within [min, max] range → 100.
    Each percentage point outside the range costs 5 points for that category.
    Final score is a weighted average using ideal percentages as weights.
    """
    breakdown = []
    weighted_score_sum = 0.0
    total_weight = 0.0

    for category in best_practices:
        cat_id = category["CategoryId"]
        ideal = category["IdealPercentage"]
        min_pct = category["MinPercentage"]
        max_pct = category["MaxPercentage"]
        cat_total = category_totals.get(cat_id, 0)

        actual_pct = (cat_total / total_spending * 100) if total_spending > 0 else 0
        actual_pct = round(actual_pct, 1)

        # Calculate category score
        if min_pct <= actual_pct <= max_pct:
            cat_score = 100
            status = "on_track"
        elif actual_pct < min_pct:
            cat_score = max(0, 100 - (min_pct - actual_pct) * 5)
            status = "under_budget"
        else:
            cat_score = max(0, 100 - (actual_pct - max_pct) * 5)
            status = "over_budget"

        weighted_score_sum += cat_score * ideal
        total_weight += ideal

        breakdown.append({
            "category_id": cat_id,
            "category_name": category["CategoryName"],
            "actual_amount": round(cat_total, 2),
            "actual_percentage": actual_pct,
            "ideal_percentage": ideal,
            "min_percentage": min_pct,
            "max_percentage": max_pct,
            "category_score": round(cat_score, 1),
            "status": status,
        })

    final_score = round(weighted_score_sum / total_weight) if total_weight > 0 else 0
    return final_score, breakdown


@tool
async def analyze_spending(consent_ids: list[str], config: RunnableConfig) -> str:
    """Analyze spending health across all connected bank accounts. Fetches transactions from Leafy Bank (internal) and ALL external banks (one per consent_id), classifies any uncategorized transactions using MongoDB Atlas Vector Search, and returns a single aggregated spending score (0-100) with per-category breakdown. Per-bank external data (accounts, loans, repayment history) is included in `banks_analyzed`.

    Args:
        consent_ids: List of consent IDs — one per connected external bank
    """
    user_id = config["configurable"]["user_id"]
    profile = config["configurable"].get("profile")

    # Stream writer for real-time progress updates to the frontend.
    # Falls back to no-op when called via ainvoke (non-streaming endpoints).
    try:
        writer = get_stream_writer()
    except Exception:
        def writer(_): pass

    try:
        # --- Step 1: Fetch all data sources concurrently ---
        token = await get_bearer_token(user_id)
        bank_count = len(consent_ids)
        writer({"type": "progress", "step": "fetch",
                "message": f"Fetching transactions from Leafy Bank and {bank_count} external bank(s)...",
                "mongodb_feature": "ISO 20022",
                "input": json.dumps({"GET": [
                    f"/leafybank/transactions/spending/{user_id}",
                    *[f"/openfinance/customers/{user_id}/external-data?consent_id={cid}"
                      for cid in consent_ids],
                    "/leafybank/spending/best-practices",
                ]})})

        internal_task = http_client.get(
            f"/leafybank/transactions/secure/spending/{user_id}",
        )
        best_practices_task = http_client.get("/leafybank/spending/best-practices")

        # Build one external fetch task per consent
        external_tasks = []
        for cid in consent_ids:
            ext_params = {"consent_id": cid}
            if profile:
                ext_params["profile"] = profile
            task = http_client.get(
                f"/openfinance/secure/customers/{user_id}/external-data",
                params=ext_params,
                headers={"Authorization": f"Bearer {token}"},
            )
            external_tasks.append((cid, task))

        # Run all tasks concurrently
        all_tasks = [internal_task, best_practices_task] + [t for _, t in external_tasks]
        all_results = await asyncio.gather(*all_tasks, return_exceptions=True)

        internal_resp = all_results[0]
        bp_resp = all_results[1]
        external_results = list(zip(
            [cid for cid, _ in external_tasks],
            all_results[2:],
        ))

        # Internal + best practices must succeed
        if isinstance(internal_resp, Exception):
            raise internal_resp
        internal_resp.raise_for_status()
        if isinstance(bp_resp, Exception):
            raise bp_resp
        bp_resp.raise_for_status()

        internal_data = internal_resp.json()
        best_practices = bp_resp.json()
        internal_transactions = internal_data.get("transactions", []) or []
        best_practices_list = best_practices.get("categories", []) or []

        # Process external responses — graceful per-bank failure
        banks_analyzed = []
        all_external_transactions = []
        errors = []

        for cid, resp in external_results:
            if isinstance(resp, Exception):
                logger.warning(f"External data fetch failed for consent {cid}: {resp}")
                errors.append({"consent_id": cid, "error": str(resp)})
                continue
            try:
                resp.raise_for_status()
                data = resp.json()
                txns = data.get("transactions", []) or []
                all_external_transactions.extend(txns)
                banks_analyzed.append({
                    "consent_id": cid,
                    "institution": data.get("source_institution", ""),
                    "transaction_count": len(txns),
                    "accounts": data.get("accounts"),
                    "products": data.get("products"),
                    "repayment_history": data.get("repayment_history"),
                    "consent_status": data.get("consent_status"),
                    "purpose": data.get("purpose"),
                    "status": "success",
                })
            except Exception as e:
                logger.warning(f"External data fetch failed for consent {cid}: {e}")
                errors.append({"consent_id": cid, "error": str(e)})

        if not banks_analyzed:
            error_details = "; ".join(e["error"] for e in errors) if errors else "unknown"
            return f"Error analyzing spending: No external bank data could be retrieved. Errors: {error_details}"

        # Build per-bank summary for progress output
        banks_summary = {}
        for bank in banks_analyzed:
            banks_summary[bank["institution"]] = {
                "accounts": len(bank.get("accounts") or []),
                "products": len(bank.get("products") or []),
                "transactions": f"[{bank['transaction_count']} items]",
                "repayment_history": len(bank.get("repayment_history") or []),
                "consent_status": bank.get("consent_status"),
            }

        # Include a sample raw ISO 20022 transaction for demo visibility
        fetch_output = {
            f"/leafybank/transactions/spending/{user_id}": {
                "transactions": f"[{len(internal_transactions)} items]",
            },
            "external_banks": banks_summary,
            "errors": errors,
            "/leafybank/spending/best-practices": {
                "categories": f"[{len(best_practices_list)} items]",
            },
        }
        if all_external_transactions:
            fetch_output["iso20022_sample_transaction"] = all_external_transactions[0]

        writer({"type": "progress", "step": "fetch",
                "output": json.dumps(fetch_output)})

        total_ext_txn_count = len(all_external_transactions)
        logger.info(
            f"Fetched {len(internal_transactions)} internal txns, "
            f"{total_ext_txn_count} external txns from {len(banks_analyzed)} bank(s), "
            f"{len(best_practices_list)} categories"
        )

        # --- Step 2: Categorize all transactions by MCC ---
        mcc_map = _build_mcc_to_category(best_practices_list)
        total_txns = len(internal_transactions) + total_ext_txn_count

        writer({"type": "progress", "step": "categorize",
                "message": f"Categorizing {total_txns} transactions by MCC code...",
                "input": json.dumps({
                    "internal_transactions": len(internal_transactions),
                    "external_transactions": total_ext_txn_count,
                    "banks_analyzed": len(banks_analyzed),
                    "mcc_codes_in_lookup": len(mcc_map),
                })})

        all_transactions = internal_transactions + all_external_transactions
        merged_totals, total_spending, uncategorized = _categorize_transactions(
            all_transactions, mcc_map
        )

        writer({"type": "progress", "step": "categorize",
                "output": json.dumps({
                    "category_totals": {k: round(v, 2) for k, v in merged_totals.items()},
                    "uncategorized": len(uncategorized),
                    "total_spending": round(total_spending, 2),
                })})

        # --- Step 3: Classify uncategorized transactions via vector search ---
        classification_summary = {
            "total_uncategorized": len(uncategorized),
            "newly_classified": 0,
            "still_uncategorized": len(uncategorized),
        }

        if uncategorized:
            classify_body = {"transactions": uncategorized}
            writer({"type": "progress", "step": "classify",
                    "message": f"Classifying {len(uncategorized)} untagged transactions...",
                    "mongodb_feature": "Vector Search",
                    "input": json.dumps({
                        "POST": "/leafybank/mcc/classify",
                        "body": classify_body,
                    })})
            logger.info(
                f"Classifying {len(uncategorized)} uncategorized transactions via vector search"
            )
            try:
                classify_resp = await http_client.post(
                    "/leafybank/mcc/classify",
                    json=classify_body,
                )
                classify_resp.raise_for_status()
                classifications = classify_resp.json().get("classifications", [])

                # Merge classified amounts into category totals
                newly_classified = 0
                still_uncat = 0
                for txn in classifications:
                    cat_id = txn.get("CategoryId", "")
                    amount = txn.get("amount", 0)

                    if cat_id and cat_id != "uncategorized":
                        merged_totals[cat_id] = merged_totals.get(cat_id, 0) + amount
                        newly_classified += 1
                    else:
                        # Still uncategorized after vector search — add to "other"
                        # so the amount is accounted for in the score calculation
                        merged_totals["other"] = merged_totals.get("other", 0) + amount
                        still_uncat += 1

                classification_summary["newly_classified"] = newly_classified
                classification_summary["still_uncategorized"] = still_uncat

                writer({"type": "progress", "step": "classify",
                        "output": json.dumps(classify_resp.json())})

                logger.info(
                    f"Classified {newly_classified}/{len(uncategorized)} transactions"
                )

            except Exception as e:
                logger.warning(f"Classification failed, scoring without it: {e}")
                # Fallback: add uncategorized amounts to "other" so they're still
                # accounted for in total_spending. Without this, these amounts vanish
                # from merged_totals and the score is artificially inflated.
                fallback_total = sum(t.get("amount", 0) for t in uncategorized)
                if fallback_total > 0:
                    merged_totals["other"] = merged_totals.get("other", 0) + fallback_total
                    logger.info(
                        f"Added {len(uncategorized)} uncategorized transactions "
                        f"(${fallback_total:.2f}) to 'other' as fallback"
                    )
                classification_summary["classification_failed"] = True
        else:
            logger.info("All transactions have MCC codes — classification not needed")

        # --- Step 4: Calculate final score (post-classification) ---
        writer({"type": "progress", "step": "score",
                "message": "Computing final spending score...",
                "input": json.dumps({
                    "category_totals": {k: round(v, 2) for k, v in merged_totals.items()},
                    "total_spending": round(total_spending, 2),
                    "best_practices_count": len(best_practices_list),
                })})
        score, breakdown = _calculate_score(
            merged_totals, total_spending, best_practices_list
        )
        writer({"type": "progress", "step": "score",
                "output": json.dumps({
                    "spending_score": score,
                    "category_breakdown": breakdown,
                })})

        result = {
            "spending_score": score,
            "total_spending": round(total_spending, 2),
            "internal_transaction_count": len(internal_transactions),
            "external_transaction_count": total_ext_txn_count,
            "category_breakdown": breakdown,
            "classification_summary": classification_summary,
            "banks_analyzed": banks_analyzed,
            "errors": errors,
        }

        return json.dumps(result)

    except httpx.HTTPStatusError as e:
        logger.error(f"Error analyzing spending: {e.response.text}")
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = e.response.text or str(e)
        return f"Error analyzing spending (HTTP {e.response.status_code}): {detail}"
    except Exception as e:
        logger.error(f"Error analyzing spending: {e}")
        return f"Error analyzing spending: {str(e)}"
