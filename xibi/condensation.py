from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

@dataclass
class CondensedContent:
    ref_id: str          # stable identifier, e.g. "email-a1b2c3d4"
    source: str          # "email" | "telegram" | "chat"
    condensed: str       # stripped content for LLM consumption (≤ 2000 chars)
    link_count: int      # number of URLs found in original
    attachment_count: int
    phishing_flag: bool  # True if any phishing signal detected
    phishing_reason: str # empty string if no flag, else short description
    truncated: bool      # True if original was truncated to fit the 2000-char cap

def condense(
    content: str,
    source: str = "email",
    ref_id: str | None = None,
) -> CondensedContent:
    """
    Strip noise from channel content. Returns a CondensedContent ready for LLM consumption.

    - content: raw text (email body, Telegram message, etc.)
    - source: channel name, used as ref_id prefix if ref_id is None
    - ref_id: if provided, used as-is; otherwise generated from content hash

    Never raises. On any error, returns CondensedContent with condensed=content[:2000].
    """
    try:
        if content is None:
            content = ""

        if not isinstance(content, str):
            content = str(content)

        # Step A: Assign ref_id
        actual_ref_id = ref_id
        if actual_ref_id is None:
            hash8 = hashlib.md5(content.encode("utf-8")).hexdigest()[:8]
            actual_ref_id = f"{source}-{hash8}"

        # Step B: Count and remove URLs
        url_pattern = re.compile(r"https?://\S+")
        link_count = len(url_pattern.findall(content))
        condensed = url_pattern.sub("[link]", content)

        # Step C: Strip email boilerplate (only if source is email)
        if source == "email":
            # 1. Strip forwarding headers
            # -----Original Message-----
            condensed = re.split(r"-----Original Message-----", condensed, flags=re.IGNORECASE)[0]

            # From:.*Sent:.*To:.*Subject:
            # Using DOTALL to match across lines
            condensed = re.split(r"From:.*?Sent:.*?To:.*?Subject:", condensed, flags=re.IGNORECASE | re.DOTALL)[0]

            # On .* wrote:
            condensed = re.split(r"On .* wrote:", condensed, flags=re.IGNORECASE)[0]

            # >+ .* (quoted lines)
            condensed = "\n".join([line for line in condensed.splitlines() if not re.match(r"^>+", line.strip())])

            # 2. Strip legal footers
            deny_list = [
                "confidentiality notice", "this email and any", "unsubscribe",
                "privacy policy", "this message is intended", "disclaimer:", "all rights reserved"
            ]
            paragraphs = condensed.split("\n\n")
            clean_paragraphs = []
            for p in paragraphs:
                if not any(phrase in p.lower() for phrase in deny_list):
                    clean_paragraphs.append(p)
            condensed = "\n\n".join(clean_paragraphs)

            # 3. Strip signature blocks
            sigs = ["\n--\n", "\nBest,\n", "\nThanks,\n", "\nRegards,\n", "\nSincerely,\n"]
            for sig in sigs:
                idx = condensed.rfind(sig)
                if idx != -1:
                    # check if in last 30%
                    if idx > len(condensed) * 0.7:
                        condensed = condensed[:idx]
                        break

        # Step D: Strip excess whitespace
        # Strip leading/trailing whitespace per line
        condensed = "\n".join([line.strip() for line in condensed.splitlines()])
        # Collapse 3+ consecutive blank lines into 2
        condensed = re.sub(r"\n{3,}", "\n\n", condensed)
        # Strip leading/trailing whitespace from the whole document
        condensed = condensed.strip()

        # Step E: Detect phishing signals
        phishing_flag = False
        phishing_reason = ""

        # Display/domain mismatch
        known_brands = ["PayPal", "Apple", "Microsoft", "Google", "Amazon", "IRS", "Bank"]
        # Extract from From: header if present in content
        from_match = re.search(r"From: (.*?)\s*<([^>]+)>", content, re.IGNORECASE)
        display_name = ""
        domain = ""

        if from_match:
            display_name = from_match.group(1).strip()
            email_addr = from_match.group(2).strip()
            if "@" in email_addr:
                domain = email_addr.split("@")[-1].lower()
        else:
            # Try matching From: email@domain.com
            from_match = re.search(r"From: (\S+@\S+)", content, re.IGNORECASE)
            if from_match:
                email_addr = from_match.group(1).strip()
                display_name = email_addr
                if "@" in email_addr:
                    domain = email_addr.split("@")[-1].lower()

        if display_name and domain:
            for brand in known_brands:
                if brand.lower() in display_name.lower():
                    if not (domain.endswith(f"{brand.lower()}.com") or domain.endswith(f"{brand.lower()}.net")):
                        phishing_flag = True
                        phishing_reason = f"Brand mismatch: {brand} display name with {domain} domain."
                        break

        # Urgency + wire transfer language
        if not phishing_flag:
            urgency_phrases = ["urgent", "immediately", "within 24 hours", "time sensitive"]
            financial_phrases = ["wire transfer", "gift card", "bitcoin", "send money", "bank account"]
            content_lower = content.lower()
            has_urgency = any(u in content_lower for u in urgency_phrases)
            has_financial = any(f in content_lower for f in financial_phrases)
            if has_urgency and has_financial:
                phishing_flag = True
                phishing_reason = "Urgent language combined with financial request."

        # CEO impersonation
        if not phishing_flag:
            ceo_pattern = r"From: .*?\((CEO|President|Director)\)"
            financial_phrases = ["wire transfer", "gift card", "bitcoin", "send money", "bank account"]
            if re.search(ceo_pattern, content, re.IGNORECASE):
                content_lower = content.lower()
                if any(f in content_lower for f in financial_phrases):
                    phishing_flag = True
                    phishing_reason = "Possible CEO impersonation with financial request."

        # Step F: Truncate to cap
        truncated = False
        if len(condensed) > 2000:
            truncated = True
            # Truncate at a word boundary (last space before 2000 chars)
            truncated_text = condensed[:2000]
            last_space = truncated_text.rfind(" ")
            if last_space != -1:
                condensed = truncated_text[:last_space]
            else:
                condensed = truncated_text

        return CondensedContent(
            ref_id=actual_ref_id,
            source=source,
            condensed=condensed,
            link_count=link_count,
            attachment_count=0,
            phishing_flag=phishing_flag,
            phishing_reason=phishing_reason,
            truncated=truncated
        )

    except Exception as e:
        # Never raises. On any error, return safe defaults.
        safe_content = str(content)[:2000] if content else ""
        return CondensedContent(
            ref_id=actual_ref_id if 'actual_ref_id' in locals() else f"{source}-error",
            source=source,
            condensed=safe_content,
            link_count=0,
            attachment_count=0,
            phishing_flag=False,
            phishing_reason="",
            truncated=len(safe_content) == 2000
        )
