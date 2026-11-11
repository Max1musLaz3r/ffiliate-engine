"""Bitcoin address validation — real, working code, no external dependencies.

Supported mainnet formats:
  * P2PKH        "1..."  — Base58Check, version byte 0x00
  * P2SH         "3..."  — Base58Check, version byte 0x05
  * Native SegWit "bc1q..." — Bech32 (BIP-173), witness version 0
  * Taproot      "bc1p..." — Bech32m (BIP-350), witness version 1

Rejected: testnet (tb1...), bad checksums, wrong lengths, bad characters,
wrong witness versions. A malformed address can never reach the payout
gateway — invalid addresses are recorded as failed payout requests instead.
"""

# --- Base58Check -----------------------------------------------------------

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58_decode(s: str) -> bytes | None:
    """Decode a Base58 string to bytes, or None on an illegal character."""
    n = 0
    for ch in s:
        try:
            digit = _B58_ALPHABET.index(ch)
        except ValueError:
            return None
        n = n * 58 + digit
    # Count leading '1's -> leading zero bytes.
    pad = len(s) - len(s.lstrip("1"))
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * pad + raw


def _base58check_decode(s: str) -> tuple[int, bytes] | None:
    """Return (version_byte, 20-byte payload) or None if the checksum fails."""
    import hashlib

    decoded = _b58_decode(s)
    if decoded is None or len(decoded) != 25:
        return None
    payload, checksum = decoded[:-4], decoded[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    if digest[:4] != checksum:
        return None
    return decoded[0], decoded[1:21]


# --- Bech32 / Bech32m (BIP-173 / BIP-350) -----------------------------------

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_GEN = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
_BECH32_CONST = 1            # BIP-173
_BECH32M_CONST = 0x2BC830A3   # BIP-350


def _bech32_polymod(values: list[int]) -> int:
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            if (b >> i) & 1:
                chk ^= _BECH32_GEN[i]
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_verify_checksum(hrp: str, data: list[int]) -> int | None:
    """Return 1 for Bech32, 2 for Bech32m, None if the checksum is invalid."""
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const == _BECH32_CONST:
        return 1
    if const == _BECH32M_CONST:
        return 2
    return None


def _bech32_decode(bech: str) -> tuple[str, list[int], int] | None:
    """Return (hrp, data_without_checksum, spec) or None on any failure."""
    if bech.lower() != bech and bech.upper() != bech:
        return None  # mixed case is invalid
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech) or len(bech) > 90:
        return None
    hrp = bech[:pos]
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        return None
    try:
        data = [_BECH32_CHARSET.index(x) for x in bech[pos + 1:]]
    except ValueError:
        return None
    spec = _bech32_verify_checksum(hrp, data)
    if spec is None:
        return None
    return hrp, data[:-6], spec


def _convertbits(data: list[int], frombits: int, tobits: int, pad: bool = True) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


# --- Public API ------------------------------------------------------------

def validate_btc_address(address: str) -> tuple[bool, str | None, str]:
    """Validate a mainnet Bitcoin address.

    Returns (is_valid, address_type, reason).
    address_type is one of: P2PKH | P2SH | P2WPKH (Native SegWit) | P2TR (Taproot).
    """
    if not address or not isinstance(address, str):
        return False, None, "empty address"

    addr = address.strip()

    # --- Base58Check: P2PKH (1...) and P2SH (3...) -------------------------
    if addr[0] in ("1", "3"):
        if len(addr) < 26 or len(addr) > 35:
            return False, None, "base58 address has invalid length"
        decoded = _base58check_decode(addr)
        if decoded is None:
            return False, None, "base58 checksum failed"
        version, _payload = decoded
        if addr[0] == "1" and version == 0x00:
            return True, "P2PKH", "valid"
        if addr[0] == "3" and version == 0x05:
            return True, "P2SH", "valid"
        return False, None, "base58 version byte does not match address prefix"

    # --- Bech32 / Bech32m: bc1q... (SegWit v0) and bc1p... (Taproot v1) -----
    if addr.lower().startswith("bc1"):
        if addr.lower().startswith("tb1"):
            return False, None, "testnet address rejected (mainnet only)"
        decoded = _bech32_decode(addr)
        if decoded is None:
            return False, None, "bech32 checksum failed"
        hrp, data, spec = decoded
        if hrp != "bc":
            return False, None, "bech32 human-readable part is not 'bc'"
        if not data:
            return False, None, "bech32 data missing witness version"
        version = data[0]
        program = _convertbits(data[1:], 5, 8, pad=False)
        if program is None:
            return False, None, "bech32 witness program could not be decoded"
        if version == 0:
            if spec != 1:
                return False, None, "witness v0 must use Bech32, not Bech32m"
            if len(addr) != 42:
                return False, None, "segwit v0 address must be 42 characters"
            if len(program) not in (20, 32):
                return False, None, "segwit v0 program must be 20 or 32 bytes"
            return True, "P2WPKH", "valid"
        if version == 1:
            if spec != 2:
                return False, None, "witness v1 (Taproot) must use Bech32m"
            if len(addr) != 62:
                return False, None, "taproot address must be 62 characters"
            if len(program) != 32:
                return False, None, "taproot program must be 32 bytes"
            return True, "P2TR", "valid"
        return False, None, f"unsupported witness version {version}"

    if addr.lower().startswith("tb1"):
        return False, None, "testnet address rejected (mainnet only)"
    return False, None, "unrecognised address format"
