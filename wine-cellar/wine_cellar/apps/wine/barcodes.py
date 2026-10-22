"""Normalise retail barcodes to EAN-13.

UPC-A is 12 digits (EAN-13 with a leading 0). GTIN-14 with a leading 0 is
EAN-13 with a logistics digit of 0. We store and look up the 13-digit form
so a gun scan, a phone photo and a typed code all hit the same wine.

A 12-digit code is only treated as UPC-A if its check digit is valid —
otherwise it is left alone (some cellar rows are incomplete scans).
"""


def _upc_a_check_ok(digits12: str) -> bool:
    total = 0
    for i, ch in enumerate(digits12[:11]):
        n = int(ch)
        total += n * 3 if i % 2 == 0 else n
    return (10 - total % 10) % 10 == int(digits12[11])


def gs1_check_ok(digits: str) -> bool:
    """True when `digits` is 8/12/13/14 long and the GS1 check digit matches."""
    if not digits or not str(digits).isdigit():
        return False
    raw = str(digits)
    if len(raw) not in (8, 12, 13, 14):
        return False
    body, check = raw[:-1], int(raw[-1])
    total = 0
    for i, ch in enumerate(reversed(body)):
        total += int(ch) * (3 if i % 2 == 0 else 1)
    return (10 - total % 10) % 10 == check


def accepted_barcode(value: str | None) -> str | None:
    """GS1-valid retail barcode, normalised, or None."""
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if not gs1_check_ok(digits):
        return None
    return normalise_barcode(digits) or digits


def normalise_barcode(value: str | None) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) == 12:
        if _upc_a_check_ok(digits):
            return "0" + digits
        return digits
    if len(digits) == 14 and digits.startswith("0"):
        return digits[1:]
    return digits


def barcode_lookup_variants(value: str | None) -> set[str]:
    """Forms that might be sitting in the DB for the same bottle."""
    raw = "".join(ch for ch in str(value or "") if ch.isdigit())
    ean = normalise_barcode(value)
    variants = {raw, ean}
    if len(ean) == 13:
        variants.add("0" + ean)
        if ean.startswith("0"):
            variants.add(ean[1:])
    variants.discard("")
    return variants
