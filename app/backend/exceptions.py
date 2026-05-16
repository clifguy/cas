"""CAS App-originated exceptions.

These inherit from ``sage.api.errors.SAGEError`` so the
``register_exception_handlers`` registration on the FastAPI app
serializes them through the shared ``ErrorResponse`` envelope.
"""

from __future__ import annotations

from sage.api.errors import SAGEError


class InvalidDirectoryError(SAGEError):
    """400: scan target directory does not exist or is not readable."""

    def __init__(self, directory: str) -> None:
        super().__init__(
            "invalid_directory",
            f"Directory '{directory}' not found or not readable",
            400,
            {"directory": directory},
        )


class EmptyFileListError(SAGEError):
    """400: ingest request specified an empty files list."""

    def __init__(self) -> None:
        super().__init__(
            "empty_file_list",
            "No files selected for ingestion",
            400,
        )
