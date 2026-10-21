"""Finite tokenizer work, independent of a provider's model token budget."""

# Character length can be checked without constructing UTF-8 bytes or tokenizer
# state. UTF-8 and pretokenization allocations are then bounded even for a long
# WordPiece word represented by one [UNK]. Larger ordinary sections are divided
# before reaching this boundary; exceptional undivided inputs fail explicitly.
MAX_TOKENIZATION_CHARACTERS = 32768


def require_bounded_tokenizer_input(text: str) -> None:
    """Refuse unsafe model work before tokenization, without clipping any text."""
    require_bounded_tokenizer_length(len(text))


def require_bounded_tokenizer_length(length: int) -> None:
    """Check combined input lengths before allocating their joined string."""
    if length > MAX_TOKENIZATION_CHARACTERS:
        raise ValueError(
            f"Embedding input exceeds tokenizer work bound of "
            f"{MAX_TOKENIZATION_CHARACTERS} characters ({length} received); "
            "the retained source is unchanged; divide the exceptional input before retrying"
        )
