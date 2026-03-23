"""Cross-validate author affiliations between API data and PDF-extracted data.

Strategy:
- Match authors by name (fuzzy, handles Chinese/English variants)
- PDF affiliation = publication-time truth (preferred)
- API affiliation = current affiliation (may have changed)
- Merge: PDF > API Author-level > API paper-level > empty
"""
import re
from typing import List, Optional


class AffiliationValidator:
    """Cross-validate and merge author data from API and PDF sources."""

    def validate(self, api_authors: List[dict], pdf_authors: List[dict]) -> List[dict]:
        """Merge API authors with PDF-extracted authors.

        For each API author:
        - If matched in PDF -> use PDF affiliation (publication-time truth)
        - If not matched -> keep API affiliation
        Unmatched PDF authors are appended.

        Returns: merged author list with 'affiliation_source' tag.
        """
        if not pdf_authors:
            return api_authors
        if not api_authors:
            return [{"name": a["name"], "affiliation": a.get("affiliation", ""),
                      "country": "", "affiliation_source": "pdf"}
                    for a in pdf_authors]

        # Build PDF lookup by name variants
        pdf_by_keys: dict = {}  # name_key -> pdf_author
        for a in pdf_authors:
            for key in self._name_keys(a.get("name", "")):
                if key not in pdf_by_keys:
                    pdf_by_keys[key] = a

        matched_pdf_names = set()
        merged = []
        for api_a in api_authors:
            enriched = dict(api_a)
            api_keys = self._name_keys(api_a.get("name", ""))

            # Try to find PDF match
            pdf_match = None
            for k in api_keys:
                if k in pdf_by_keys:
                    pdf_match = pdf_by_keys[k]
                    matched_pdf_names.update(self._name_keys(pdf_match.get("name", "")))
                    break

            if pdf_match:
                # PDF affiliation takes priority (publication-time truth)
                pdf_affil = pdf_match.get("affiliation", "").strip()
                if pdf_affil:
                    enriched["affiliation"] = pdf_affil
                    enriched["affiliation_source"] = "pdf"
                else:
                    enriched["affiliation_source"] = "api"
                # Also grab email if PDF has it
                if pdf_match.get("email"):
                    enriched["email"] = pdf_match["email"]
            else:
                enriched["affiliation_source"] = "api"

            merged.append(enriched)

        # Append unmatched PDF authors (API missed them)
        for pdf_a in pdf_authors:
            pdf_keys = self._name_keys(pdf_a.get("name", ""))
            if not (pdf_keys & matched_pdf_names):
                merged.append({
                    "name": pdf_a["name"],
                    "affiliation": pdf_a.get("affiliation", ""),
                    "email": pdf_a.get("email", ""),
                    "country": "",
                    "affiliation_source": "pdf_only",
                })

        return merged

    @staticmethod
    def _name_keys(name: str) -> set:
        """Extract all name variants for matching (same logic as scholar dedup)."""
        keys = set()
        cleaned = name.strip()
        if not cleaned:
            return keys
        # Split on parentheses and slashes
        parts = re.split(r'[()（）/／]', cleaned)
        for part in parts:
            p = part.strip().strip(',，、').strip()
            if p and len(p) >= 2:
                keys.add(p.lower())
        base = re.sub(r'[（(].*?[）)]', '', cleaned).strip()
        if base and len(base) >= 2:
            keys.add(base.lower())
        return keys
