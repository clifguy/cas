"""Generated from the Core OpenAPI error schemas; do not edit."""
# ruff: noqa: E501 -- generated schema strings retain authoritative prose

import json

SCHEMAS = json.loads(
    r"""{
  "AdapterConfigInvalidError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "adapter_config_invalid",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/AdapterConfigInvalidErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "AdapterConfigInvalidErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "key": {
        "description": "Key.",
        "type": "string"
      },
      "source_type": {
        "description": "Source type.",
        "type": "string"
      },
      "value": {
        "description": "The rejected JSON value, echoed without successful-input constraints."
      }
    },
    "required": [
      "source_type",
      "key",
      "value"
    ],
    "type": "object"
  },
  "AdapterNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "adapter_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "AmbiguousDocumentIdentifierError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "ambiguous_document_identifier",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/AmbiguousDocumentIdentifierErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "AmbiguousDocumentIdentifierErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "supplied": {
        "description": "Supplied.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "tool": {
        "description": "Tool.",
        "type": "string"
      }
    },
    "required": [
      "tool",
      "supplied"
    ],
    "type": "object"
  },
  "AmbiguousIngestSourceError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "ambiguous_ingest_source",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "AssertionsFileInvalidError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "assertions_file_invalid",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/AssertionsFileInvalidErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "AssertionsFileInvalidErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "assertions_file": {
        "description": "Assertions file.",
        "type": "string"
      },
      "reason": {
        "description": "Reason.",
        "type": "string"
      }
    },
    "required": [
      "assertions_file",
      "reason"
    ],
    "type": "object"
  },
  "AssertionsFileNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "assertions_file_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/AssertionsFileNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "AssertionsFileNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "assertions_file": {
        "description": "Assertions file.",
        "type": "string"
      }
    },
    "required": [
      "assertions_file"
    ],
    "type": "object"
  },
  "AssertionsNotConfiguredError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "assertions_not_configured",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "AuthFailedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "auth_failed",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "AuthNotConfiguredError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "auth_not_configured",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "AuthRequiredError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "auth_required",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "BinaryContentRefusedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "binary_content_refused",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/BinaryContentRefusedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object",
    "x-mcp-tools": [
      "get_document"
    ]
  },
  "BinaryContentRefusedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "source_type": {
        "description": "Source type.",
        "type": "string"
      },
      "use_instead": {
        "description": "Use instead.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "source_type",
      "use_instead"
    ],
    "type": "object"
  },
  "CallerFilesystemUnavailableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "caller_filesystem_unavailable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/CallerFilesystemUnavailableErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "CallerFilesystemUnavailableErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "operation": {
        "description": "Operation.",
        "type": "string"
      },
      "remedy": {
        "description": "Remedy.",
        "type": "string"
      }
    },
    "required": [
      "operation",
      "remedy"
    ],
    "type": "object"
  },
  "ContentDeliveryConflictError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "content_delivery_conflict",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "ContentFileMissingError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "content_file_missing",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ContentFileMissingErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ContentFileMissingErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "source_path": {
        "description": "Source path.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "source_path"
    ],
    "type": "object"
  },
  "ContentTooLargeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "content_too_large",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ContentTooLargeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ContentTooLargeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "max_bytes": {
        "description": "Max bytes.",
        "type": "integer"
      },
      "size_bytes": {
        "description": "Size bytes.",
        "type": "integer"
      }
    },
    "required": [
      "document_id",
      "size_bytes",
      "max_bytes"
    ],
    "type": "object"
  },
  "DeliveryConflictError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "delivery_conflict",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DeliveryConflictErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DeliveryConflictErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "delivery": {
        "description": "Delivery.",
        "type": "string"
      },
      "reason": {
        "description": "Reason.",
        "type": "string"
      }
    },
    "required": [
      "delivery",
      "reason"
    ],
    "type": "object"
  },
  "DestructiveConfigChangeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "destructive_config_change",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DestructiveConfigChangeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DestructiveConfigChangeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "warnings": {
        "description": "Warnings.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "warnings"
    ],
    "type": "object"
  },
  "DocumentNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "document_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DocumentNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DocumentNotFoundErrorDetail": {
    "description": "Additional context for this refusal.",
    "oneOf": [
      {
        "additionalProperties": false,
        "properties": {
          "document_id": {
            "description": "Document id.",
            "type": "string"
          }
        },
        "required": [
          "document_id"
        ],
        "type": "object"
      },
      {
        "additionalProperties": false,
        "properties": {
          "document_id": {
            "description": "Document id.",
            "type": "string"
          },
          "ever_existed": {
            "description": "Ever existed.",
            "type": "boolean"
          },
          "id_well_formed": {
            "description": "Id well formed.",
            "type": "boolean"
          },
          "slug_matches_catalog": {
            "description": "Slug matches catalog.",
            "type": "boolean"
          }
        },
        "required": [
          "document_id",
          "id_well_formed",
          "ever_existed",
          "slug_matches_catalog"
        ],
        "type": "object"
      }
    ]
  },
  "DocumentScopeUnmatchedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "document_scope_unmatched",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DocumentScopeUnmatchedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DocumentScopeUnmatchedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "unmatched_ids": {
        "description": "Unmatched ids.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "unmatched_ids"
    ],
    "type": "object"
  },
  "DownloadUrlUnavailableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "download_url_unavailable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DownloadUrlUnavailableErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DownloadUrlUnavailableErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      }
    },
    "required": [
      "document_id"
    ],
    "type": "object"
  },
  "DuplicateContentError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "duplicate_content",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/DuplicateContentErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "DuplicateContentErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "existing_document_id": {
        "description": "Existing document id.",
        "type": "string"
      },
      "source_content_hash": {
        "description": "Source content hash.",
        "type": "string"
      }
    },
    "required": [
      "existing_document_id",
      "source_content_hash"
    ],
    "type": "object"
  },
  "EdgeAnchorPolicyViolationError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "edge_anchor_policy_violation",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/EdgeAnchorPolicyViolationErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "EdgeAnchorPolicyViolationErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_type": {
        "description": "Edge type.",
        "type": "string"
      },
      "offending_fields": {
        "description": "Offending fields.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "resolution_policy": {
        "description": "Resolution policy.",
        "type": "string"
      },
      "violation": {
        "description": "Violation.",
        "type": "string"
      }
    },
    "required": [
      "edge_type",
      "resolution_policy",
      "violation"
    ],
    "type": "object"
  },
  "EdgeNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "edge_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/EdgeNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "EdgeNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_id": {
        "description": "Edge id.",
        "type": "string"
      }
    },
    "required": [
      "edge_id"
    ],
    "type": "object"
  },
  "EmptyFileListError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "empty_file_list",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "ErrorResponse": {
    "description": "Error envelope selected by code. Known codes require their declared detail; rejected input values remain arbitrary JSON. Extension codes cannot bypass a known family.",
    "discriminator": {
      "mapping": {
        "adapter_config_invalid": "#/components/schemas/AdapterConfigInvalidError",
        "adapter_not_found": "#/components/schemas/AdapterNotFoundError",
        "ambiguous_document_identifier": "#/components/schemas/AmbiguousDocumentIdentifierError",
        "ambiguous_ingest_source": "#/components/schemas/AmbiguousIngestSourceError",
        "assertions_file_invalid": "#/components/schemas/AssertionsFileInvalidError",
        "assertions_file_not_found": "#/components/schemas/AssertionsFileNotFoundError",
        "assertions_not_configured": "#/components/schemas/AssertionsNotConfiguredError",
        "auth_failed": "#/components/schemas/AuthFailedError",
        "auth_not_configured": "#/components/schemas/AuthNotConfiguredError",
        "auth_required": "#/components/schemas/AuthRequiredError",
        "binary_content_refused": "#/components/schemas/BinaryContentRefusedError",
        "caller_filesystem_unavailable": "#/components/schemas/CallerFilesystemUnavailableError",
        "content_delivery_conflict": "#/components/schemas/ContentDeliveryConflictError",
        "content_file_missing": "#/components/schemas/ContentFileMissingError",
        "content_too_large": "#/components/schemas/ContentTooLargeError",
        "delivery_conflict": "#/components/schemas/DeliveryConflictError",
        "destructive_config_change": "#/components/schemas/DestructiveConfigChangeError",
        "document_not_found": "#/components/schemas/DocumentNotFoundError",
        "document_scope_unmatched": "#/components/schemas/DocumentScopeUnmatchedError",
        "download_url_unavailable": "#/components/schemas/DownloadUrlUnavailableError",
        "duplicate_content": "#/components/schemas/DuplicateContentError",
        "edge_anchor_policy_violation": "#/components/schemas/EdgeAnchorPolicyViolationError",
        "edge_not_found": "#/components/schemas/EdgeNotFoundError",
        "empty_file_list": "#/components/schemas/EmptyFileListError",
        "expected_head_version_requires_predecessor": "#/components/schemas/ExpectedHeadVersionRequiresPredecessorError",
        "force_reingest_path_mismatch": "#/components/schemas/ForceReingestPathMismatchError",
        "force_reingest_pin_mismatch": "#/components/schemas/ForceReingestPinMismatchError",
        "heading_not_found": "#/components/schemas/HeadingNotFoundError",
        "http_error": "#/components/schemas/HttpErrorError",
        "identical_content_supersede": "#/components/schemas/IdenticalContentSupersedeError",
        "internal_error": "#/components/schemas/InternalErrorError",
        "invalid_action": "#/components/schemas/InvalidActionError",
        "invalid_batch_metadata": "#/components/schemas/InvalidBatchMetadataError",
        "invalid_directory": "#/components/schemas/InvalidDirectoryError",
        "invalid_doc_type": "#/components/schemas/InvalidDocTypeError",
        "invalid_document_date": "#/components/schemas/InvalidDocumentDateError",
        "invalid_document_id": "#/components/schemas/InvalidDocumentIdError",
        "invalid_edge_id": "#/components/schemas/InvalidEdgeIdError",
        "invalid_filter_shape": "#/components/schemas/InvalidFilterShapeError",
        "invalid_filter_value": "#/components/schemas/InvalidFilterValueError",
        "invalid_lifecycle_transition": "#/components/schemas/InvalidLifecycleTransitionError",
        "invalid_mode": "#/components/schemas/InvalidModeError",
        "invalid_parameter": "#/components/schemas/InvalidParameterError",
        "invalid_sha256": "#/components/schemas/InvalidSha256Error",
        "invalid_state": "#/components/schemas/InvalidStateError",
        "invalid_user_id": "#/components/schemas/InvalidUserIdError",
        "invalid_vault_id": "#/components/schemas/InvalidVaultIdError",
        "legacy_form": "#/components/schemas/LegacyFormError",
        "lifecycle_state_not_applicable": "#/components/schemas/LifecycleStateNotApplicableError",
        "local_open_only": "#/components/schemas/LocalOpenOnlyError",
        "local_profile_only": "#/components/schemas/LocalProfileOnlyError",
        "merged_from_validation": "#/components/schemas/MergedFromValidationError",
        "method_not_allowed": "#/components/schemas/MethodNotAllowedError",
        "misplaced_filters": "#/components/schemas/MisplacedFiltersError",
        "misplaced_metadata": "#/components/schemas/MisplacedMetadataError",
        "misplaced_top_level_field": "#/components/schemas/MisplacedTopLevelFieldError",
        "missing_document_id": "#/components/schemas/MissingDocumentIdError",
        "missing_document_identifier": "#/components/schemas/MissingDocumentIdentifierError",
        "missing_heading_path": "#/components/schemas/MissingHeadingPathError",
        "missing_ingest_source": "#/components/schemas/MissingIngestSourceError",
        "missing_query": "#/components/schemas/MissingQueryError",
        "missing_relocated_to": "#/components/schemas/MissingRelocatedToError",
        "missing_successor_id": "#/components/schemas/MissingSuccessorIdError",
        "mode_parameter_mismatch": "#/components/schemas/ModeParameterMismatchError",
        "no_projection": "#/components/schemas/NoProjectionError",
        "output_path_invalid": "#/components/schemas/OutputPathInvalidError",
        "patch_empty": "#/components/schemas/PatchEmptyError",
        "path_traversal_denied": "#/components/schemas/PathTraversalDeniedError",
        "pipeline_incomplete": "#/components/schemas/PipelineIncompleteError",
        "pipeline_work_in_flight": "#/components/schemas/PipelineWorkInFlightError",
        "reabstract_already_in_flight": "#/components/schemas/ReabstractAlreadyInFlightError",
        "reabstract_document_already_in_flight": "#/components/schemas/ReabstractDocumentAlreadyInFlightError",
        "recompute_pipeline_already_in_flight": "#/components/schemas/RecomputePipelineAlreadyInFlightError",
        "relocated_from_provenance_mismatch": "#/components/schemas/RelocatedFromProvenanceMismatchError",
        "relocated_to_provenance_mismatch": "#/components/schemas/RelocatedToProvenanceMismatchError",
        "relocation_source_undelivered": "#/components/schemas/RelocationSourceUndeliveredError",
        "reserved_transition": "#/components/schemas/ReservedTransitionError",
        "restore_provenance_mismatch": "#/components/schemas/RestoreProvenanceMismatchError",
        "restore_source_not_absolute": "#/components/schemas/RestoreSourceNotAbsoluteError",
        "restore_target_unresolved": "#/components/schemas/RestoreTargetUnresolvedError",
        "retract_target_not_edge": "#/components/schemas/RetractTargetNotEdgeError",
        "route_not_found": "#/components/schemas/RouteNotFoundError",
        "sage_upstream_timeout": "#/components/schemas/SageUpstreamTimeoutError",
        "sage_upstream_unavailable": "#/components/schemas/SageUpstreamUnavailableError",
        "self_referential_edge": "#/components/schemas/SelfReferentialEdgeError",
        "source_digest_mismatch": "#/components/schemas/SourceDigestMismatchError",
        "source_file_not_found": "#/components/schemas/SourceFileNotFoundError",
        "source_type_unresolved": "#/components/schemas/SourceTypeUnresolvedError",
        "source_unreadable": "#/components/schemas/SourceUnreadableError",
        "staging_edge_not_found": "#/components/schemas/StagingEdgeNotFoundError",
        "stale_chain_head": "#/components/schemas/StaleChainHeadError",
        "stale_read": "#/components/schemas/StaleReadError",
        "storage_query_failed": "#/components/schemas/StorageQueryFailedError",
        "supersede_target_not_active": "#/components/schemas/SupersedeTargetNotActiveError",
        "synced_from_inapplicable_edge_type": "#/components/schemas/SyncedFromInapplicableEdgeTypeError",
        "synced_from_version_not_in_source_chain": "#/components/schemas/SyncedFromVersionNotInSourceChainError",
        "tag_patch_overlap": "#/components/schemas/TagPatchOverlapError",
        "tags_add_conflict": "#/components/schemas/TagsAddConflictError",
        "tags_remove_conflict": "#/components/schemas/TagsRemoveConflictError",
        "tbd_policy_edge": "#/components/schemas/TbdPolicyEdgeError",
        "tier3_doc_type_change_stale_keys": "#/components/schemas/Tier3DocTypeChangeStaleKeysError",
        "tier3_patch_overlap": "#/components/schemas/Tier3PatchOverlapError",
        "tier3_schema_violation": "#/components/schemas/Tier3SchemaViolationError",
        "tier3_unique_constraint_violation": "#/components/schemas/Tier3UniqueConstraintViolationError",
        "tier3_unset_conflict": "#/components/schemas/Tier3UnsetConflictError",
        "transfer_content_too_large": "#/components/schemas/TransferContentTooLargeError",
        "transfer_endpoint_not_configured": "#/components/schemas/TransferEndpointNotConfiguredError",
        "transfer_not_staged": "#/components/schemas/TransferNotStagedError",
        "transfer_refusal_limit_reached": "#/components/schemas/TransferRefusalLimitReachedError",
        "transfer_token_already_used": "#/components/schemas/TransferTokenAlreadyUsedError",
        "transfer_token_invalid": "#/components/schemas/TransferTokenInvalidError",
        "undeclared_key": "#/components/schemas/UndeclaredKeyError",
        "unexpected_relocated_to": "#/components/schemas/UnexpectedRelocatedToError",
        "unexpected_successor_id": "#/components/schemas/UnexpectedSuccessorIdError",
        "unknown_filter_key": "#/components/schemas/UnknownFilterKeyError",
        "unknown_parameter": "#/components/schemas/UnknownParameterError",
        "vault_already_exists": "#/components/schemas/VaultAlreadyExistsError",
        "vault_config_validation_error": "#/components/schemas/VaultConfigValidationErrorError",
        "vault_migration_in_flight": "#/components/schemas/VaultMigrationInFlightError",
        "vault_not_found": "#/components/schemas/VaultNotFoundError",
        "vault_source_path_refused": "#/components/schemas/VaultSourcePathRefusedError",
        "vault_source_store_refused": "#/components/schemas/VaultSourceStoreRefusedError",
        "vault_source_store_unavailable": "#/components/schemas/VaultSourceStoreUnavailableError",
        "write_path_exists": "#/components/schemas/WritePathExistsError",
        "write_path_invalid": "#/components/schemas/WritePathInvalidError"
      },
      "propertyName": "code"
    },
    "oneOf": [
      {
        "$ref": "#/components/schemas/AdapterConfigInvalidError"
      },
      {
        "$ref": "#/components/schemas/AdapterNotFoundError"
      },
      {
        "$ref": "#/components/schemas/AmbiguousDocumentIdentifierError"
      },
      {
        "$ref": "#/components/schemas/AmbiguousIngestSourceError"
      },
      {
        "$ref": "#/components/schemas/AssertionsFileInvalidError"
      },
      {
        "$ref": "#/components/schemas/AssertionsFileNotFoundError"
      },
      {
        "$ref": "#/components/schemas/AssertionsNotConfiguredError"
      },
      {
        "$ref": "#/components/schemas/AuthFailedError"
      },
      {
        "$ref": "#/components/schemas/AuthNotConfiguredError"
      },
      {
        "$ref": "#/components/schemas/AuthRequiredError"
      },
      {
        "$ref": "#/components/schemas/BinaryContentRefusedError"
      },
      {
        "$ref": "#/components/schemas/CallerFilesystemUnavailableError"
      },
      {
        "$ref": "#/components/schemas/ContentDeliveryConflictError"
      },
      {
        "$ref": "#/components/schemas/ContentFileMissingError"
      },
      {
        "$ref": "#/components/schemas/ContentTooLargeError"
      },
      {
        "$ref": "#/components/schemas/DeliveryConflictError"
      },
      {
        "$ref": "#/components/schemas/DestructiveConfigChangeError"
      },
      {
        "$ref": "#/components/schemas/DocumentNotFoundError"
      },
      {
        "$ref": "#/components/schemas/DocumentScopeUnmatchedError"
      },
      {
        "$ref": "#/components/schemas/DownloadUrlUnavailableError"
      },
      {
        "$ref": "#/components/schemas/DuplicateContentError"
      },
      {
        "$ref": "#/components/schemas/EdgeAnchorPolicyViolationError"
      },
      {
        "$ref": "#/components/schemas/EdgeNotFoundError"
      },
      {
        "$ref": "#/components/schemas/EmptyFileListError"
      },
      {
        "$ref": "#/components/schemas/ExpectedHeadVersionRequiresPredecessorError"
      },
      {
        "$ref": "#/components/schemas/ForceReingestPathMismatchError"
      },
      {
        "$ref": "#/components/schemas/ForceReingestPinMismatchError"
      },
      {
        "$ref": "#/components/schemas/HeadingNotFoundError"
      },
      {
        "$ref": "#/components/schemas/HttpErrorError"
      },
      {
        "$ref": "#/components/schemas/IdenticalContentSupersedeError"
      },
      {
        "$ref": "#/components/schemas/InternalErrorError"
      },
      {
        "$ref": "#/components/schemas/InvalidActionError"
      },
      {
        "$ref": "#/components/schemas/InvalidBatchMetadataError"
      },
      {
        "$ref": "#/components/schemas/InvalidDirectoryError"
      },
      {
        "$ref": "#/components/schemas/InvalidDocTypeError"
      },
      {
        "$ref": "#/components/schemas/InvalidDocumentDateError"
      },
      {
        "$ref": "#/components/schemas/InvalidDocumentIdError"
      },
      {
        "$ref": "#/components/schemas/InvalidEdgeIdError"
      },
      {
        "$ref": "#/components/schemas/InvalidFilterShapeError"
      },
      {
        "$ref": "#/components/schemas/InvalidFilterValueError"
      },
      {
        "$ref": "#/components/schemas/InvalidLifecycleTransitionError"
      },
      {
        "$ref": "#/components/schemas/InvalidModeError"
      },
      {
        "$ref": "#/components/schemas/InvalidParameterError"
      },
      {
        "$ref": "#/components/schemas/InvalidSha256Error"
      },
      {
        "$ref": "#/components/schemas/InvalidStateError"
      },
      {
        "$ref": "#/components/schemas/InvalidUserIdError"
      },
      {
        "$ref": "#/components/schemas/InvalidVaultIdError"
      },
      {
        "$ref": "#/components/schemas/LegacyFormError"
      },
      {
        "$ref": "#/components/schemas/LifecycleStateNotApplicableError"
      },
      {
        "$ref": "#/components/schemas/LocalOpenOnlyError"
      },
      {
        "$ref": "#/components/schemas/MergedFromValidationError"
      },
      {
        "$ref": "#/components/schemas/MethodNotAllowedError"
      },
      {
        "$ref": "#/components/schemas/MisplacedFiltersError"
      },
      {
        "$ref": "#/components/schemas/MisplacedMetadataError"
      },
      {
        "$ref": "#/components/schemas/MisplacedTopLevelFieldError"
      },
      {
        "$ref": "#/components/schemas/MissingDocumentIdError"
      },
      {
        "$ref": "#/components/schemas/MissingDocumentIdentifierError"
      },
      {
        "$ref": "#/components/schemas/MissingHeadingPathError"
      },
      {
        "$ref": "#/components/schemas/MissingIngestSourceError"
      },
      {
        "$ref": "#/components/schemas/MissingQueryError"
      },
      {
        "$ref": "#/components/schemas/MissingRelocatedToError"
      },
      {
        "$ref": "#/components/schemas/MissingSuccessorIdError"
      },
      {
        "$ref": "#/components/schemas/ModeParameterMismatchError"
      },
      {
        "$ref": "#/components/schemas/NoProjectionError"
      },
      {
        "$ref": "#/components/schemas/OutputPathInvalidError"
      },
      {
        "$ref": "#/components/schemas/PatchEmptyError"
      },
      {
        "$ref": "#/components/schemas/PathTraversalDeniedError"
      },
      {
        "$ref": "#/components/schemas/PipelineIncompleteError"
      },
      {
        "$ref": "#/components/schemas/PipelineWorkInFlightError"
      },
      {
        "$ref": "#/components/schemas/ReabstractAlreadyInFlightError"
      },
      {
        "$ref": "#/components/schemas/ReabstractDocumentAlreadyInFlightError"
      },
      {
        "$ref": "#/components/schemas/RecomputePipelineAlreadyInFlightError"
      },
      {
        "$ref": "#/components/schemas/RelocatedFromProvenanceMismatchError"
      },
      {
        "$ref": "#/components/schemas/RelocatedToProvenanceMismatchError"
      },
      {
        "$ref": "#/components/schemas/RelocationSourceUndeliveredError"
      },
      {
        "$ref": "#/components/schemas/ReservedTransitionError"
      },
      {
        "$ref": "#/components/schemas/RestoreProvenanceMismatchError"
      },
      {
        "$ref": "#/components/schemas/RestoreSourceNotAbsoluteError"
      },
      {
        "$ref": "#/components/schemas/RestoreTargetUnresolvedError"
      },
      {
        "$ref": "#/components/schemas/RetractTargetNotEdgeError"
      },
      {
        "$ref": "#/components/schemas/RouteNotFoundError"
      },
      {
        "$ref": "#/components/schemas/SageUpstreamTimeoutError"
      },
      {
        "$ref": "#/components/schemas/SageUpstreamUnavailableError"
      },
      {
        "$ref": "#/components/schemas/LocalProfileOnlyError"
      },
      {
        "$ref": "#/components/schemas/SelfReferentialEdgeError"
      },
      {
        "$ref": "#/components/schemas/SourceDigestMismatchError"
      },
      {
        "$ref": "#/components/schemas/SourceFileNotFoundError"
      },
      {
        "$ref": "#/components/schemas/SourceTypeUnresolvedError"
      },
      {
        "$ref": "#/components/schemas/SourceUnreadableError"
      },
      {
        "$ref": "#/components/schemas/StagingEdgeNotFoundError"
      },
      {
        "$ref": "#/components/schemas/StaleChainHeadError"
      },
      {
        "$ref": "#/components/schemas/StaleReadError"
      },
      {
        "$ref": "#/components/schemas/StorageQueryFailedError"
      },
      {
        "$ref": "#/components/schemas/SupersedeTargetNotActiveError"
      },
      {
        "$ref": "#/components/schemas/SyncedFromInapplicableEdgeTypeError"
      },
      {
        "$ref": "#/components/schemas/SyncedFromVersionNotInSourceChainError"
      },
      {
        "$ref": "#/components/schemas/TagPatchOverlapError"
      },
      {
        "$ref": "#/components/schemas/TagsAddConflictError"
      },
      {
        "$ref": "#/components/schemas/TagsRemoveConflictError"
      },
      {
        "$ref": "#/components/schemas/TbdPolicyEdgeError"
      },
      {
        "$ref": "#/components/schemas/Tier3DocTypeChangeStaleKeysError"
      },
      {
        "$ref": "#/components/schemas/Tier3PatchOverlapError"
      },
      {
        "$ref": "#/components/schemas/Tier3SchemaViolationError"
      },
      {
        "$ref": "#/components/schemas/Tier3UniqueConstraintViolationError"
      },
      {
        "$ref": "#/components/schemas/Tier3UnsetConflictError"
      },
      {
        "$ref": "#/components/schemas/TransferContentTooLargeError"
      },
      {
        "$ref": "#/components/schemas/TransferEndpointNotConfiguredError"
      },
      {
        "$ref": "#/components/schemas/TransferNotStagedError"
      },
      {
        "$ref": "#/components/schemas/TransferRefusalLimitReachedError"
      },
      {
        "$ref": "#/components/schemas/TransferTokenAlreadyUsedError"
      },
      {
        "$ref": "#/components/schemas/TransferTokenInvalidError"
      },
      {
        "$ref": "#/components/schemas/UnexpectedRelocatedToError"
      },
      {
        "$ref": "#/components/schemas/UnexpectedSuccessorIdError"
      },
      {
        "$ref": "#/components/schemas/UndeclaredKeyError"
      },
      {
        "$ref": "#/components/schemas/UnknownFilterKeyError"
      },
      {
        "$ref": "#/components/schemas/UnknownParameterError"
      },
      {
        "$ref": "#/components/schemas/VaultAlreadyExistsError"
      },
      {
        "$ref": "#/components/schemas/VaultConfigValidationErrorError"
      },
      {
        "$ref": "#/components/schemas/VaultMigrationInFlightError"
      },
      {
        "$ref": "#/components/schemas/VaultNotFoundError"
      },
      {
        "$ref": "#/components/schemas/VaultSourcePathRefusedError"
      },
      {
        "$ref": "#/components/schemas/VaultSourceStoreRefusedError"
      },
      {
        "$ref": "#/components/schemas/VaultSourceStoreUnavailableError"
      },
      {
        "$ref": "#/components/schemas/WritePathExistsError"
      },
      {
        "$ref": "#/components/schemas/WritePathInvalidError"
      },
      {
        "$ref": "#/components/schemas/ExtensionError"
      }
    ],
    "properties": {
      "code": {
        "description": "Machine-readable error code (e.g., \"invalid_lifecycle_transition\", \"document_not_found\", \"editor_permission_denied\").",
        "type": "string"
      },
      "detail": {
        "additionalProperties": true,
        "description": "Additional context. Structure varies by error type (e.g., current_state and attempted_action for lifecycle transition errors).",
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object",
    "x-mcp-tool-errors": {
      "bulk_ingest_document": [
        "adapter_config_invalid",
        "adapter_not_found",
        "ambiguous_ingest_source",
        "document_not_found",
        "duplicate_content",
        "edge_anchor_policy_violation",
        "empty_file_list",
        "expected_head_version_requires_predecessor",
        "force_reingest_path_mismatch",
        "force_reingest_pin_mismatch",
        "identical_content_supersede",
        "internal_error",
        "invalid_action",
        "invalid_doc_type",
        "invalid_document_date",
        "invalid_document_id",
        "invalid_edge_id",
        "invalid_lifecycle_transition",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_user_id",
        "invalid_vault_id",
        "legacy_form",
        "merged_from_validation",
        "missing_ingest_source",
        "missing_relocated_to",
        "missing_successor_id",
        "relocated_from_provenance_mismatch",
        "relocated_to_provenance_mismatch",
        "relocation_source_undelivered",
        "reserved_transition",
        "retract_target_not_edge",
        "self_referential_edge",
        "source_digest_mismatch",
        "source_file_not_found",
        "source_type_unresolved",
        "source_unreadable",
        "stale_chain_head",
        "supersede_target_not_active",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "tier3_schema_violation",
        "tier3_unique_constraint_violation",
        "transfer_endpoint_not_configured",
        "transfer_not_staged",
        "transfer_token_invalid",
        "undeclared_key",
        "unexpected_relocated_to",
        "unexpected_successor_id",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found",
        "vault_source_path_refused",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "chain": [
        "ambiguous_document_identifier",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "unknown_parameter",
        "vault_not_found"
      ],
      "create_edges": [
        "document_not_found",
        "edge_anchor_policy_violation",
        "internal_error",
        "invalid_document_date",
        "invalid_document_id",
        "invalid_edge_id",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_user_id",
        "invalid_vault_id",
        "legacy_form",
        "merged_from_validation",
        "retract_target_not_edge",
        "self_referential_edge",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "undeclared_key",
        "unknown_parameter",
        "vault_not_found"
      ],
      "create_vault": [
        "internal_error",
        "invalid_parameter",
        "unknown_parameter",
        "vault_already_exists",
        "vault_config_validation_error"
      ],
      "delete_edge": [
        "edge_not_found",
        "internal_error",
        "invalid_edge_id",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "export_projection": [
        "caller_filesystem_unavailable",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "no_projection",
        "output_path_invalid",
        "path_traversal_denied",
        "unknown_parameter",
        "vault_not_found"
      ],
      "get_default_vault_config": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter"
      ],
      "get_document": [
        "ambiguous_document_identifier",
        "binary_content_refused",
        "content_delivery_conflict",
        "content_file_missing",
        "content_too_large",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "transfer_endpoint_not_configured",
        "unknown_parameter",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable",
        "write_path_exists",
        "write_path_invalid"
      ],
      "get_filename_metadata": [
        "adapter_not_found",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "get_stack_config": [
        "internal_error",
        "invalid_parameter",
        "unknown_parameter"
      ],
      "get_vault_config": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "get_vault_stats": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "ingest_document": [
        "adapter_config_invalid",
        "adapter_not_found",
        "ambiguous_ingest_source",
        "document_not_found",
        "duplicate_content",
        "edge_anchor_policy_violation",
        "expected_head_version_requires_predecessor",
        "force_reingest_path_mismatch",
        "force_reingest_pin_mismatch",
        "identical_content_supersede",
        "internal_error",
        "invalid_action",
        "invalid_doc_type",
        "invalid_document_date",
        "invalid_document_id",
        "invalid_lifecycle_transition",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_user_id",
        "invalid_vault_id",
        "legacy_form",
        "lifecycle_state_not_applicable",
        "merged_from_validation",
        "misplaced_metadata",
        "misplaced_top_level_field",
        "missing_ingest_source",
        "missing_relocated_to",
        "missing_successor_id",
        "relocated_from_provenance_mismatch",
        "relocated_to_provenance_mismatch",
        "relocation_source_undelivered",
        "reserved_transition",
        "retract_target_not_edge",
        "self_referential_edge",
        "source_digest_mismatch",
        "source_file_not_found",
        "source_type_unresolved",
        "source_unreadable",
        "stale_chain_head",
        "supersede_target_not_active",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "tier3_schema_violation",
        "tier3_unique_constraint_violation",
        "transfer_endpoint_not_configured",
        "transfer_not_staged",
        "transfer_token_invalid",
        "undeclared_key",
        "unexpected_relocated_to",
        "unexpected_successor_id",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found",
        "vault_source_path_refused",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "list_directory": [
        "caller_filesystem_unavailable",
        "internal_error",
        "invalid_directory",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "list_headings": [
        "ambiguous_document_identifier",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "no_projection",
        "unknown_parameter",
        "vault_not_found"
      ],
      "list_pending_metadata": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "list_staging_edges": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "list_vaults": [
        "internal_error",
        "invalid_parameter",
        "unknown_parameter"
      ],
      "migrate_vault": [
        "force_reingest_path_mismatch",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "pipeline_work_in_flight",
        "source_file_not_found",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found"
      ],
      "optimize_vault_content_store": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "read_projection": [
        "ambiguous_document_identifier",
        "delivery_conflict",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "no_projection",
        "transfer_endpoint_not_configured",
        "unknown_parameter",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable",
        "write_path_exists",
        "write_path_invalid"
      ],
      "read_section": [
        "ambiguous_document_identifier",
        "document_not_found",
        "heading_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "no_projection",
        "unknown_parameter",
        "vault_not_found"
      ],
      "recompute_abstract": [
        "adapter_not_found",
        "document_not_found",
        "edge_anchor_policy_violation",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "merged_from_validation",
        "no_projection",
        "reabstract_document_already_in_flight",
        "retract_target_not_edge",
        "self_referential_edge",
        "source_file_not_found",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "recompute_deferred_vault_abstracts": [
        "adapter_not_found",
        "document_not_found",
        "edge_anchor_policy_violation",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "merged_from_validation",
        "no_projection",
        "reabstract_already_in_flight",
        "reabstract_document_already_in_flight",
        "retract_target_not_edge",
        "self_referential_edge",
        "source_file_not_found",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "recompute_pipeline": [
        "adapter_config_invalid",
        "adapter_not_found",
        "document_not_found",
        "edge_anchor_policy_violation",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "merged_from_validation",
        "recompute_pipeline_already_in_flight",
        "retract_target_not_edge",
        "self_referential_edge",
        "source_file_not_found",
        "source_unreadable",
        "synced_from_inapplicable_edge_type",
        "synced_from_version_not_in_source_chain",
        "tbd_policy_edge",
        "unknown_parameter",
        "vault_migration_in_flight",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "recompute_views": [
        "caller_filesystem_unavailable",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "reload_vault": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_config_validation_error",
        "vault_not_found"
      ],
      "restore_vault_source_file": [
        "ambiguous_ingest_source",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_vault_id",
        "missing_ingest_source",
        "restore_provenance_mismatch",
        "restore_source_not_absolute",
        "restore_target_unresolved",
        "source_digest_mismatch",
        "source_file_not_found",
        "transfer_endpoint_not_configured",
        "transfer_not_staged",
        "transfer_token_invalid",
        "unknown_parameter",
        "vault_not_found",
        "vault_source_path_refused",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "search": [
        "document_not_found",
        "heading_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_filter_shape",
        "invalid_filter_value",
        "invalid_mode",
        "invalid_parameter",
        "invalid_vault_id",
        "legacy_form",
        "misplaced_filters",
        "missing_document_id",
        "missing_heading_path",
        "missing_query",
        "mode_parameter_mismatch",
        "pipeline_incomplete",
        "storage_query_failed",
        "tier3_schema_violation",
        "unknown_filter_key",
        "unknown_parameter",
        "vault_not_found"
      ],
      "traverse": [
        "ambiguous_document_identifier",
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "missing_document_identifier",
        "tbd_policy_edge",
        "unknown_parameter",
        "vault_not_found"
      ],
      "update_lifecycles": [
        "ambiguous_document_identifier",
        "document_not_found",
        "identical_content_supersede",
        "internal_error",
        "invalid_action",
        "invalid_document_date",
        "invalid_document_id",
        "invalid_lifecycle_transition",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_user_id",
        "invalid_vault_id",
        "legacy_form",
        "missing_document_identifier",
        "missing_relocated_to",
        "missing_successor_id",
        "relocated_to_provenance_mismatch",
        "reserved_transition",
        "supersede_target_not_active",
        "undeclared_key",
        "unexpected_relocated_to",
        "unexpected_successor_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "update_metadata": [
        "ambiguous_document_identifier",
        "document_not_found",
        "internal_error",
        "invalid_doc_type",
        "invalid_document_date",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_user_id",
        "invalid_vault_id",
        "legacy_form",
        "lifecycle_state_not_applicable",
        "missing_document_identifier",
        "patch_empty",
        "stale_read",
        "tag_patch_overlap",
        "tags_add_conflict",
        "tags_remove_conflict",
        "tier3_doc_type_change_stale_keys",
        "tier3_patch_overlap",
        "tier3_schema_violation",
        "tier3_unset_conflict",
        "undeclared_key",
        "unknown_parameter",
        "vault_not_found"
      ],
      "update_staging_edge": [
        "internal_error",
        "invalid_action",
        "invalid_edge_id",
        "invalid_parameter",
        "invalid_vault_id",
        "staging_edge_not_found",
        "unknown_parameter",
        "vault_not_found"
      ],
      "update_vault_config": [
        "destructive_config_change",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_config_validation_error",
        "vault_not_found"
      ],
      "verify_hashes": [
        "internal_error",
        "invalid_parameter",
        "invalid_sha256",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "verify_preconditions": [
        "document_not_found",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "verify_vault_drift": [
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found"
      ],
      "verify_vault_retrieval": [
        "assertions_file_invalid",
        "assertions_file_not_found",
        "assertions_not_configured",
        "internal_error",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ],
      "verify_vault_source_files": [
        "document_scope_unmatched",
        "internal_error",
        "invalid_document_id",
        "invalid_parameter",
        "invalid_vault_id",
        "unknown_parameter",
        "vault_not_found",
        "vault_source_store_refused",
        "vault_source_store_unavailable"
      ]
    }
  },
  "ExpectedHeadVersionRequiresPredecessorError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "expected_head_version_requires_predecessor",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "ExtensionError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "description": "Machine-readable error code selecting this envelope.",
        "not": {
          "enum": [
            "adapter_config_invalid",
            "adapter_not_found",
            "ambiguous_document_identifier",
            "ambiguous_ingest_source",
            "assertions_file_invalid",
            "assertions_file_not_found",
            "assertions_not_configured",
            "auth_failed",
            "auth_not_configured",
            "auth_required",
            "binary_content_refused",
            "caller_filesystem_unavailable",
            "content_delivery_conflict",
            "content_file_missing",
            "content_too_large",
            "delivery_conflict",
            "destructive_config_change",
            "document_not_found",
            "document_scope_unmatched",
            "download_url_unavailable",
            "duplicate_content",
            "edge_anchor_policy_violation",
            "edge_not_found",
            "empty_file_list",
            "expected_head_version_requires_predecessor",
            "force_reingest_path_mismatch",
            "force_reingest_pin_mismatch",
            "heading_not_found",
            "http_error",
            "identical_content_supersede",
            "internal_error",
            "invalid_action",
            "invalid_batch_metadata",
            "invalid_directory",
            "invalid_doc_type",
            "invalid_document_date",
            "invalid_document_id",
            "invalid_edge_id",
            "invalid_filter_shape",
            "invalid_filter_value",
            "invalid_lifecycle_transition",
            "invalid_mode",
            "invalid_parameter",
            "invalid_sha256",
            "invalid_state",
            "invalid_user_id",
            "invalid_vault_id",
            "legacy_form",
            "lifecycle_state_not_applicable",
            "local_open_only",
            "merged_from_validation",
            "method_not_allowed",
            "misplaced_filters",
            "misplaced_metadata",
            "misplaced_top_level_field",
            "missing_document_id",
            "missing_document_identifier",
            "missing_heading_path",
            "missing_ingest_source",
            "missing_query",
            "missing_relocated_to",
            "missing_successor_id",
            "mode_parameter_mismatch",
            "no_projection",
            "output_path_invalid",
            "patch_empty",
            "path_traversal_denied",
            "pipeline_incomplete",
            "pipeline_work_in_flight",
            "reabstract_already_in_flight",
            "reabstract_document_already_in_flight",
            "recompute_pipeline_already_in_flight",
            "relocated_from_provenance_mismatch",
            "relocated_to_provenance_mismatch",
            "relocation_source_undelivered",
            "reserved_transition",
            "restore_provenance_mismatch",
            "restore_source_not_absolute",
            "restore_target_unresolved",
            "retract_target_not_edge",
            "route_not_found",
            "sage_upstream_timeout",
            "sage_upstream_unavailable",
            "local_profile_only",
            "self_referential_edge",
            "source_digest_mismatch",
            "source_file_not_found",
            "source_type_unresolved",
            "source_unreadable",
            "staging_edge_not_found",
            "stale_chain_head",
            "stale_read",
            "storage_query_failed",
            "supersede_target_not_active",
            "synced_from_inapplicable_edge_type",
            "synced_from_version_not_in_source_chain",
            "tag_patch_overlap",
            "tags_add_conflict",
            "tags_remove_conflict",
            "tbd_policy_edge",
            "tier3_doc_type_change_stale_keys",
            "tier3_patch_overlap",
            "tier3_schema_violation",
            "tier3_unique_constraint_violation",
            "tier3_unset_conflict",
            "transfer_content_too_large",
            "transfer_endpoint_not_configured",
            "transfer_not_staged",
            "transfer_refusal_limit_reached",
            "transfer_token_already_used",
            "transfer_token_invalid",
            "unexpected_relocated_to",
            "unexpected_successor_id",
            "undeclared_key",
            "unknown_filter_key",
            "unknown_parameter",
            "vault_already_exists",
            "vault_config_validation_error",
            "vault_migration_in_flight",
            "vault_not_found",
            "vault_source_path_refused",
            "vault_source_store_refused",
            "vault_source_store_unavailable",
            "write_path_exists",
            "write_path_invalid"
          ]
        },
        "type": "string"
      },
      "detail": {
        "additionalProperties": true,
        "description": "No additional context is emitted for this error.",
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "ForceReingestPathMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "force_reingest_path_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ForceReingestPathMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ForceReingestPathMismatchErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "existing_document_id": {
        "description": "Existing document id.",
        "type": "string"
      },
      "existing_source_path": {
        "description": "Existing source path.",
        "type": "string"
      },
      "new_source_path": {
        "description": "New source path.",
        "type": "string"
      },
      "source_content_hash": {
        "description": "Source content hash.",
        "type": "string"
      }
    },
    "required": [
      "existing_document_id",
      "existing_source_path",
      "new_source_path",
      "source_content_hash"
    ],
    "type": "object"
  },
  "ForceReingestPinMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "force_reingest_pin_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ForceReingestPinMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ForceReingestPinMismatchErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "existing_document_id": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "description": "Existing document id."
      },
      "pinned_source_content_hash": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "description": "Pinned source content hash."
      },
      "source_content_hash": {
        "description": "Source content hash.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "pinned_source_content_hash",
      "source_content_hash",
      "existing_document_id"
    ],
    "type": "object"
  },
  "HeadingNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "heading_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/HeadingNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "HeadingNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "available_headings": {
        "description": "Available headings.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "candidate_matches": {
        "description": "Candidate matches.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "heading_path": {
        "description": "Heading path.",
        "type": "string"
      }
    },
    "required": [
      "heading_path",
      "document_id"
    ],
    "type": "object"
  },
  "HttpErrorError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "http_error",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "IdenticalContentSupersedeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "identical_content_supersede",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/IdenticalContentSupersedeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "IdenticalContentSupersedeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "predecessor_id": {
        "description": "Predecessor id.",
        "type": "string"
      },
      "source_content_hash": {
        "description": "Source content hash.",
        "type": "string"
      }
    },
    "required": [
      "predecessor_id",
      "source_content_hash"
    ],
    "type": "object"
  },
  "InternalErrorError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "internal_error",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "InvalidActionError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_action",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidActionErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidActionErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "attempted_action": {
        "description": "Attempted action.",
        "type": "string"
      },
      "known_actions": {
        "description": "Known actions.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "attempted_action"
    ],
    "type": "object"
  },
  "InvalidBatchMetadataError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_batch_metadata",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "InvalidDirectoryError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_directory",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "anyOf": [
          {
            "additionalProperties": false,
            "properties": {
              "directory": {
                "description": "The directory the caller requested.",
                "type": "string"
              }
            },
            "required": [
              "directory"
            ],
            "type": "object"
          },
          {
            "type": "null"
          }
        ],
        "description": "The refused directory on the Application API; omitted by the MCP tool."
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "InvalidDocTypeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_doc_type",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidDocTypeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidDocTypeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "doc_type": {
        "description": "Doc type.",
        "type": "string"
      },
      "valid_types": {
        "description": "Valid types.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "doc_type",
      "valid_types"
    ],
    "type": "object"
  },
  "InvalidDocumentDateError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_document_date",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidDocumentDateErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidDocumentDateErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_date": {
        "description": "Document date."
      },
      "expected": {
        "description": "Expected.",
        "type": "string"
      }
    },
    "required": [
      "document_date",
      "expected"
    ],
    "type": "object"
  },
  "InvalidDocumentIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_document_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidDocumentIdErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidDocumentIdErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id."
      }
    },
    "required": [
      "document_id"
    ],
    "type": "object"
  },
  "InvalidEdgeIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_edge_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidEdgeIdErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidEdgeIdErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_id": {
        "description": "Edge id."
      },
      "expected": {
        "description": "Expected.",
        "type": "string"
      }
    },
    "required": [
      "edge_id",
      "expected"
    ],
    "type": "object"
  },
  "InvalidFilterShapeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_filter_shape",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidFilterShapeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidFilterShapeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "expected_type": {
        "description": "Expected type.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "received_type": {
        "description": "Received type.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "expected_type",
      "received_type"
    ],
    "type": "object"
  },
  "InvalidFilterValueError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_filter_value",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidFilterValueErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidFilterValueErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "valid_values": {
        "description": "Valid values.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "value": {
        "description": "The rejected JSON value, echoed without successful-input constraints."
      }
    },
    "required": [
      "field",
      "value",
      "valid_values"
    ],
    "type": "object"
  },
  "InvalidLifecycleTransitionError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_lifecycle_transition",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidLifecycleTransitionErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidLifecycleTransitionErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "attempted_action": {
        "description": "Attempted action.",
        "type": "string"
      },
      "current_state": {
        "description": "Current state.",
        "type": "string"
      },
      "pipeline_status": {
        "description": "Pipeline status.",
        "type": "string"
      },
      "valid_actions": {
        "description": "Valid actions.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "current_state",
      "attempted_action",
      "valid_actions"
    ],
    "type": "object"
  },
  "InvalidModeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_mode",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidModeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidModeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "mode": {
        "description": "Mode.",
        "type": "string"
      },
      "valid_modes": {
        "description": "Valid modes.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "mode",
      "valid_modes"
    ],
    "type": "object"
  },
  "InvalidParameterError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_parameter",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidParameterErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidParameterErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "constraint": {
        "description": "Constraint.",
        "type": "string"
      },
      "hint": {
        "description": "Hint.",
        "type": "string"
      },
      "parameter": {
        "description": "Parameter.",
        "type": "string"
      },
      "value": {
        "description": "The rejected JSON value, echoed without successful-input constraints."
      }
    },
    "required": [
      "parameter",
      "value",
      "constraint"
    ],
    "type": "object"
  },
  "InvalidSha256Error": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_sha256",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidSha256ErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidSha256ErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "maxProperties": 2,
    "minProperties": 2,
    "patternProperties": {
      "^files\\.[0-9]+\\.sha256$": {}
    },
    "properties": {
      "expected": {
        "description": "Expected.",
        "type": "string"
      },
      "sha256": {
        "description": "Sha256."
      }
    },
    "required": [
      "expected"
    ],
    "type": "object"
  },
  "InvalidStateError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_state",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "InvalidUserIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_user_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidUserIdErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidUserIdErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "expected": {
        "description": "Expected.",
        "type": "string"
      },
      "user_id": {
        "description": "User id."
      }
    },
    "required": [
      "user_id",
      "expected"
    ],
    "type": "object"
  },
  "InvalidVaultIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "invalid_vault_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/InvalidVaultIdErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "InvalidVaultIdErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "expected": {
        "description": "Expected.",
        "type": "string"
      },
      "vault_id": {
        "description": "Vault id."
      }
    },
    "required": [
      "vault_id",
      "expected"
    ],
    "type": "object"
  },
  "LegacyFormError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "legacy_form",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/LegacyFormErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "LegacyFormErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "received_type": {
        "description": "Received type.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "received_type",
      "example"
    ],
    "type": "object"
  },
  "LifecycleStateNotApplicableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "lifecycle_state_not_applicable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/LifecycleStateNotApplicableErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "LifecycleStateNotApplicableErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_state": {
        "description": "Current state.",
        "type": "string"
      },
      "doc_type": {
        "description": "Doc type.",
        "type": "string"
      },
      "state_doc_types": {
        "description": "State doc types.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "current_state",
      "doc_type",
      "state_doc_types"
    ],
    "type": "object"
  },
  "LocalOpenOnlyError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "local_open_only",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "LocalProfileOnlyError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "local_profile_only",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MergedFromValidationError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "merged_from_validation",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/MergedFromValidationErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "MergedFromValidationErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source_id": {
        "description": "Source id.",
        "type": "string"
      },
      "target_id": {
        "description": "Target id.",
        "type": "string"
      },
      "violation": {
        "description": "Violation.",
        "type": "string"
      }
    },
    "required": [
      "violation"
    ],
    "type": "object"
  },
  "MethodNotAllowedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "method_not_allowed",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MisplacedFiltersError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "misplaced_filters",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/MisplacedFiltersErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "MisplacedFiltersErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "fields": {
        "description": "Fields.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "recognized": {
        "description": "Recognized.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "fields",
      "recognized",
      "example"
    ],
    "type": "object"
  },
  "MisplacedMetadataError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "misplaced_metadata",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/MisplacedMetadataErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "MisplacedMetadataErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "fields": {
        "description": "Fields.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "recognized": {
        "description": "Recognized.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "fields",
      "recognized",
      "example"
    ],
    "type": "object"
  },
  "MisplacedTopLevelFieldError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "misplaced_top_level_field",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/MisplacedTopLevelFieldErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "MisplacedTopLevelFieldErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "fields": {
        "description": "Fields.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "recognized": {
        "description": "Recognized.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "fields",
      "recognized",
      "example"
    ],
    "type": "object"
  },
  "MissingDocumentIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_document_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MissingDocumentIdentifierError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_document_identifier",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/MissingDocumentIdentifierErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "MissingDocumentIdentifierErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "accepted": {
        "description": "Accepted.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "tool": {
        "description": "Tool.",
        "type": "string"
      }
    },
    "required": [
      "tool",
      "accepted"
    ],
    "type": "object"
  },
  "MissingHeadingPathError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_heading_path",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MissingIngestSourceError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_ingest_source",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MissingQueryError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_query",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MissingRelocatedToError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_relocated_to",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "MissingSuccessorIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "missing_successor_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "ModeParameterMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "mode_parameter_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ModeParameterMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ModeParameterMismatchErrorDetail": {
    "description": "Additional context for this refusal.",
    "oneOf": [
      {
        "additionalProperties": false,
        "properties": {
          "allowed_modes": {
            "description": "Allowed modes.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "forbidden_param": {
            "description": "Forbidden param.",
            "type": "string"
          },
          "mode": {
            "description": "Mode.",
            "type": "string"
          },
          "target": {
            "description": "Target.",
            "type": "string"
          }
        },
        "required": [
          "mode",
          "target",
          "forbidden_param",
          "allowed_modes"
        ],
        "type": "object"
      },
      {
        "additionalProperties": false,
        "properties": {
          "allowed_targets": {
            "description": "Allowed targets.",
            "items": {
              "type": "string"
            },
            "type": "array"
          },
          "forbidden_param": {
            "description": "Forbidden param.",
            "type": "string"
          },
          "mode": {
            "description": "Mode.",
            "type": "string"
          },
          "target": {
            "description": "Target.",
            "type": "string"
          }
        },
        "required": [
          "mode",
          "target",
          "forbidden_param",
          "allowed_targets"
        ],
        "type": "object"
      }
    ]
  },
  "NoProjectionError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "no_projection",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/NoProjectionErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "NoProjectionErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      }
    },
    "required": [
      "document_id"
    ],
    "type": "object"
  },
  "OutputPathInvalidError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "output_path_invalid",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/OutputPathInvalidErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "OutputPathInvalidErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "output_path": {
        "description": "Output path.",
        "type": "string"
      },
      "reason": {
        "description": "Reason.",
        "type": "string"
      }
    },
    "required": [
      "output_path",
      "reason"
    ],
    "type": "object"
  },
  "PatchEmptyError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "patch_empty",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/PatchEmptyErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "PatchEmptyErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "field": {
        "description": "Field.",
        "type": "string"
      }
    },
    "required": [
      "field"
    ],
    "type": "object"
  },
  "PathTraversalDeniedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "path_traversal_denied",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/PathTraversalDeniedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "PathTraversalDeniedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "output_path": {
        "description": "Output path.",
        "type": "string"
      }
    },
    "required": [
      "output_path"
    ],
    "type": "object"
  },
  "PipelineIncompleteError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "pipeline_incomplete",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/PipelineIncompleteErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object",
    "x-mcp-tools": [
      "search"
    ]
  },
  "PipelineIncompleteErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      }
    },
    "required": [
      "document_id"
    ],
    "type": "object"
  },
  "PipelineWorkInFlightError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "pipeline_work_in_flight",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/PipelineWorkInFlightErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "PipelineWorkInFlightErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "vault_id": {
        "description": "Vault id.",
        "type": "string"
      }
    },
    "required": [
      "vault_id"
    ],
    "type": "object"
  },
  "ReabstractAlreadyInFlightError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "reabstract_already_in_flight",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ReabstractAlreadyInFlightErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ReabstractAlreadyInFlightErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "start_time": {
        "description": "Start time.",
        "type": "string"
      },
      "vault_id": {
        "description": "Vault id.",
        "type": "string"
      }
    },
    "required": [
      "vault_id",
      "start_time"
    ],
    "type": "object"
  },
  "ReabstractDocumentAlreadyInFlightError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "reabstract_document_already_in_flight",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ReabstractDocumentAlreadyInFlightErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ReabstractDocumentAlreadyInFlightErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "start_time": {
        "description": "Start time.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "start_time"
    ],
    "type": "object"
  },
  "ReadMeta": {
    "description": "Self-describing markers for a SAGE read response (CAS-ADR-039). Nested as `read_meta` on every read-path response model so a caller can tell a delivered success apart from a transport-truncated fragment, and a thin/empty body apart from a populated one. The same carrier rides the error envelope with `success=false`.",
    "properties": {
      "body_length": {
        "description": "Body length when present; null for write-to-path delivery or no-body responses.",
        "minimum": 0,
        "type": [
          "integer",
          "null"
        ]
      },
      "body_present": {
        "description": "Whether a content body is present.",
        "type": "boolean"
      },
      "projection_recovery": {
        "description": "Name of the tool that recomputes a stale projection (recompute_abstract). Populated only when projection_status is 'stale'; null otherwise. Gives a caller an actionable recovery pointer without a second probing round-trip.",
        "type": [
          "string",
          "null"
        ]
      },
      "projection_status": {
        "description": "Freshness of the projection on a projection read: 'current' when the stored projection is up to date with the source, 'stale' when the source was modified after the projection last ran (an administrative deficit, not a thin document). Null where the response is not a projection read.",
        "enum": [
          "current",
          "stale",
          null
        ],
        "type": [
          "string",
          "null"
        ]
      },
      "success": {
        "description": "True on a delivered success response; the complete sub-object's presence distinguishes a real response from a transport-truncated fragment.",
        "type": "boolean"
      }
    },
    "required": [
      "success",
      "body_present"
    ],
    "type": "object"
  },
  "RecomputePipelineAlreadyInFlightError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "recompute_pipeline_already_in_flight",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RecomputePipelineAlreadyInFlightErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RecomputePipelineAlreadyInFlightErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "start_time": {
        "description": "Start time.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "start_time"
    ],
    "type": "object"
  },
  "RelocatedFromProvenanceMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "relocated_from_provenance_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RelocatedFromProvenanceMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RelocatedFromProvenanceMismatchErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_content_hash": {
        "description": "Document content hash.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "pointer_content_hash": {
        "description": "Pointer content hash.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "pointer_content_hash",
      "document_content_hash"
    ],
    "type": "object"
  },
  "RelocatedToProvenanceMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "relocated_to_provenance_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RelocatedToProvenanceMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RelocatedToProvenanceMismatchErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "also_accounted_content_hash": {
        "description": "Also accounted content hash.",
        "type": "string"
      },
      "document_content_hash": {
        "description": "Document content hash.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "pointer_content_hash": {
        "description": "Pointer content hash.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "pointer_content_hash",
      "document_content_hash"
    ],
    "type": "object"
  },
  "RelocationSourceUndeliveredError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "relocation_source_undelivered",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RelocationSourceUndeliveredErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RelocationSourceUndeliveredErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source": {
        "description": "Source.",
        "type": "string"
      }
    },
    "required": [
      "source"
    ],
    "type": "object"
  },
  "ReservedTransitionError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "reserved_transition",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/ReservedTransitionErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "ReservedTransitionErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "attempted_action": {
        "description": "Attempted action.",
        "type": "string"
      },
      "from_state": {
        "description": "From state.",
        "type": "string"
      },
      "reason": {
        "description": "Reason.",
        "type": "string"
      },
      "to_state": {
        "description": "To state.",
        "type": "string"
      }
    },
    "required": [
      "from_state",
      "attempted_action",
      "to_state",
      "reason"
    ],
    "type": "object"
  },
  "RestoreProvenanceMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "restore_provenance_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RestoreProvenanceMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RestoreProvenanceMismatchErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "delivered_content_hash": {
        "description": "Delivered content hash.",
        "type": "string"
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "recorded_content_hash": {
        "description": "Recorded content hash.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "delivered_content_hash",
      "recorded_content_hash"
    ],
    "type": "object"
  },
  "RestoreSourceNotAbsoluteError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "restore_source_not_absolute",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RestoreSourceNotAbsoluteErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RestoreSourceNotAbsoluteErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source": {
        "description": "Source.",
        "type": "string"
      }
    },
    "required": [
      "source"
    ],
    "type": "object"
  },
  "RestoreTargetUnresolvedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "restore_target_unresolved",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RestoreTargetUnresolvedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RestoreTargetUnresolvedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "candidate_ids": {
        "description": "Candidate ids.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "content_hash": {
        "description": "Content hash.",
        "type": "string"
      }
    },
    "required": [
      "content_hash",
      "candidate_ids"
    ],
    "type": "object"
  },
  "RetractTargetNotEdgeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "retract_target_not_edge",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/RetractTargetNotEdgeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "RetractTargetNotEdgeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "retracted_edge_id": {
        "description": "Retracted edge id.",
        "type": "string"
      }
    },
    "required": [
      "retracted_edge_id"
    ],
    "type": "object"
  },
  "RouteNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "route_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "SageUpstreamTimeoutError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "sage_upstream_timeout",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "SageUpstreamUnavailableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "sage_upstream_unavailable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "SelfReferentialEdgeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "self_referential_edge",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SelfReferentialEdgeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SelfReferentialEdgeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      }
    },
    "required": [
      "document_id"
    ],
    "type": "object"
  },
  "SourceDigestMismatchError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "source_digest_mismatch",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SourceDigestMismatchErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SourceDigestMismatchErrorDetail": {
    "description": "Additional context for this refusal.",
    "oneOf": [
      {
        "additionalProperties": false,
        "properties": {
          "delivered_sha256": {
            "description": "Delivered sha256.",
            "type": [
              "string",
              "null"
            ]
          },
          "transfer_id": {
            "description": "Transfer id.",
            "type": "string"
          }
        },
        "required": [
          "transfer_id",
          "delivered_sha256"
        ],
        "type": "object"
      },
      {
        "additionalProperties": false,
        "properties": {
          "declared_sha256": {
            "description": "Declared sha256.",
            "type": [
              "string",
              "null"
            ]
          },
          "delivered_sha256": {
            "description": "Delivered sha256.",
            "type": [
              "string",
              "null"
            ]
          },
          "source": {
            "description": "Source.",
            "type": [
              "string",
              "null"
            ]
          }
        },
        "required": [
          "source",
          "declared_sha256",
          "delivered_sha256"
        ],
        "type": "object"
      }
    ]
  },
  "SourceFileNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "source_file_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SourceFileNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SourceFileNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source": {
        "description": "Source.",
        "type": "string"
      }
    },
    "required": [
      "source"
    ],
    "type": "object"
  },
  "SourceTypeUnresolvedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "source_type_unresolved",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SourceTypeUnresolvedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SourceTypeUnresolvedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "extension": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "description": "Extension."
      },
      "registered_source_types": {
        "description": "Registered source types.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "extension",
      "registered_source_types"
    ],
    "type": "object"
  },
  "SourceUnreadableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "source_unreadable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SourceUnreadableErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SourceUnreadableErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source_path": {
        "description": "Source path.",
        "type": "string"
      },
      "source_type": {
        "description": "Source type.",
        "type": "string"
      }
    },
    "required": [
      "source_type",
      "source_path"
    ],
    "type": "object"
  },
  "StagingEdgeNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "staging_edge_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/StagingEdgeNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "StagingEdgeNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_id": {
        "description": "Edge id.",
        "type": "string"
      }
    },
    "required": [
      "edge_id"
    ],
    "type": "object"
  },
  "StaleChainHeadError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "stale_chain_head",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/StaleChainHeadErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "StaleChainHeadErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_head_id": {
        "description": "Current head id.",
        "type": "string"
      },
      "current_head_version": {
        "description": "Current head version.",
        "type": "string"
      },
      "expected_head_version": {
        "description": "Expected head version.",
        "type": "string"
      },
      "predecessor_id": {
        "description": "Predecessor id.",
        "type": "string"
      }
    },
    "required": [
      "predecessor_id",
      "expected_head_version",
      "current_head_id",
      "current_head_version"
    ],
    "type": "object"
  },
  "StaleReadError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "stale_read",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/StaleReadErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "StaleReadErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_version": {
        "description": "Current version.",
        "type": "string"
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "expected_version": {
        "description": "Expected version.",
        "type": "string"
      }
    },
    "required": [
      "document_id",
      "expected_version",
      "current_version"
    ],
    "type": "object"
  },
  "StorageQueryFailedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "storage_query_failed",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/StorageQueryFailedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "StorageQueryFailedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "operation": {
        "description": "Operation.",
        "type": "string"
      }
    },
    "required": [
      "operation"
    ],
    "type": "object"
  },
  "SupersedeTargetNotActiveError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "supersede_target_not_active",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SupersedeTargetNotActiveErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SupersedeTargetNotActiveErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "allowed_states": {
        "description": "Allowed states.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "current_state": {
        "description": "Current state.",
        "type": "string"
      },
      "predecessor_id": {
        "description": "Predecessor id.",
        "type": "string"
      },
      "required_state": {
        "description": "Required state.",
        "type": "string"
      }
    },
    "required": [
      "predecessor_id",
      "current_state",
      "required_state",
      "allowed_states"
    ],
    "type": "object"
  },
  "SyncedFromInapplicableEdgeTypeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "synced_from_inapplicable_edge_type",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SyncedFromInapplicableEdgeTypeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SyncedFromInapplicableEdgeTypeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_type": {
        "description": "Edge type.",
        "type": "string"
      },
      "fields_set": {
        "description": "Fields set.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "edge_type",
      "fields_set"
    ],
    "type": "object"
  },
  "SyncedFromVersionNotInSourceChainError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "synced_from_version_not_in_source_chain",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/SyncedFromVersionNotInSourceChainErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "SyncedFromVersionNotInSourceChainErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "synced_from_version": {
        "description": "Synced from version.",
        "type": "string"
      },
      "target_id": {
        "description": "Target id.",
        "type": "string"
      }
    },
    "required": [
      "target_id",
      "synced_from_version"
    ],
    "type": "object"
  },
  "TagPatchOverlapError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tag_patch_overlap",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TagPatchOverlapErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TagPatchOverlapErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "tags": {
        "description": "Tags.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "violation": {
        "description": "Violation.",
        "type": "string"
      }
    },
    "required": [
      "violation",
      "tags"
    ],
    "type": "object"
  },
  "TagsAddConflictError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tags_add_conflict",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TagsAddConflictErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TagsAddConflictErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_tags": {
        "description": "Current tags.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "tags": {
        "description": "Tags.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "document_id",
      "tags",
      "current_tags"
    ],
    "type": "object"
  },
  "TagsRemoveConflictError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tags_remove_conflict",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TagsRemoveConflictErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object",
    "x-mcp-tools": [
      "update_metadata"
    ]
  },
  "TagsRemoveConflictErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_tags": {
        "description": "Current tags.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "tags": {
        "description": "Tags.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "document_id",
      "tags",
      "current_tags"
    ],
    "type": "object"
  },
  "TbdPolicyEdgeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tbd_policy_edge",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TbdPolicyEdgeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TbdPolicyEdgeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "edge_type": {
        "description": "Edge type.",
        "type": "string"
      }
    },
    "required": [
      "edge_type"
    ],
    "type": "object"
  },
  "Tier3DocTypeChangeStaleKeysError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tier3_doc_type_change_stale_keys",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/Tier3DocTypeChangeStaleKeysErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "Tier3DocTypeChangeStaleKeysErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "merged_tier3_keys": {
        "description": "Merged tier3 keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "new_doc_type": {
        "description": "New doc type.",
        "type": "string"
      },
      "previous_doc_type": {
        "description": "Previous doc type.",
        "type": "string"
      },
      "stale_keys": {
        "description": "Stale keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "document_id",
      "previous_doc_type",
      "new_doc_type",
      "stale_keys",
      "merged_tier3_keys"
    ],
    "type": "object"
  },
  "Tier3PatchOverlapError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tier3_patch_overlap",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/Tier3PatchOverlapErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "Tier3PatchOverlapErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "keys": {
        "description": "Keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "keys"
    ],
    "type": "object"
  },
  "Tier3SchemaViolationError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tier3_schema_violation",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/Tier3SchemaViolationErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "Tier3SchemaViolationErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "doc_type": {
        "description": "Doc type.",
        "type": "string"
      },
      "instance": {
        "description": "The rejected metadata value."
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "path": {
        "description": "Path.",
        "type": "string"
      },
      "requirements": {
        "additionalProperties": true,
        "description": "The vault-configured metadata requirements; keys and values follow that vault schema.",
        "type": "object"
      }
    },
    "required": [
      "doc_type",
      "path",
      "message"
    ],
    "type": "object"
  },
  "Tier3UniqueConstraintViolationError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tier3_unique_constraint_violation",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/Tier3UniqueConstraintViolationErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "Tier3UniqueConstraintViolationErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "colliding_value": {
        "description": "The conflicting metadata value."
      },
      "doc_type": {
        "description": "Doc type.",
        "type": "string"
      },
      "existing_document_id": {
        "description": "Existing document id.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      }
    },
    "required": [
      "doc_type",
      "field",
      "colliding_value",
      "existing_document_id"
    ],
    "type": "object"
  },
  "Tier3UnsetConflictError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "tier3_unset_conflict",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/Tier3UnsetConflictErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "Tier3UnsetConflictErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "current_tier3_keys": {
        "description": "Current tier3 keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "doc_type": {
        "anyOf": [
          {
            "type": "string"
          },
          {
            "type": "null"
          }
        ],
        "description": "Doc type."
      },
      "document_id": {
        "description": "Document id.",
        "type": "string"
      },
      "keys": {
        "description": "Keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "document_id",
      "doc_type",
      "keys",
      "current_tier3_keys"
    ],
    "type": "object"
  },
  "TransferContentTooLargeError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_content_too_large",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TransferContentTooLargeErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TransferContentTooLargeErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "max_bytes": {
        "description": "Max bytes.",
        "type": "integer"
      }
    },
    "required": [
      "max_bytes"
    ],
    "type": "object"
  },
  "TransferEndpointNotConfiguredError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_endpoint_not_configured",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "TransferNotStagedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_not_staged",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TransferNotStagedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TransferNotStagedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "transfer_id": {
        "description": "Transfer id.",
        "type": "string"
      }
    },
    "required": [
      "transfer_id"
    ],
    "type": "object"
  },
  "TransferRefusalLimitReachedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_refusal_limit_reached",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TransferRefusalLimitReachedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TransferRefusalLimitReachedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "max_refused_deliveries": {
        "description": "Max refused deliveries.",
        "type": "integer"
      },
      "transfer_id": {
        "description": "Transfer id.",
        "type": "string"
      }
    },
    "required": [
      "transfer_id",
      "max_refused_deliveries"
    ],
    "type": "object"
  },
  "TransferTokenAlreadyUsedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_token_already_used",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/TransferTokenAlreadyUsedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "TransferTokenAlreadyUsedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "transfer_id": {
        "description": "Transfer id.",
        "type": "string"
      }
    },
    "required": [
      "transfer_id"
    ],
    "type": "object"
  },
  "TransferTokenInvalidError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "transfer_token_invalid",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "description": "No additional context is emitted for this error.",
        "maxProperties": 0,
        "type": [
          "object",
          "null"
        ]
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message"
    ],
    "type": "object"
  },
  "UndeclaredKeyError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "undeclared_key",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/UndeclaredKeyErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "UndeclaredKeyErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "aliases": {
        "additionalProperties": {
          "type": "string"
        },
        "description": "Aliases.",
        "type": "object"
      },
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "key": {
        "description": "Key.",
        "type": "string"
      },
      "keys": {
        "description": "Keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "parameter": {
        "description": "Parameter.",
        "type": "string"
      },
      "recognized": {
        "description": "Recognized.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "see_also": {
        "description": "See also.",
        "items": {
          "additionalProperties": false,
          "properties": {
            "key": {
              "description": "Key.",
              "type": "string"
            },
            "location": {
              "description": "Location.",
              "type": "string"
            },
            "operation": {
              "description": "Operation.",
              "type": "string"
            }
          },
          "required": [
            "key",
            "operation",
            "location"
          ],
          "type": "object"
        },
        "type": "array"
      }
    },
    "required": [
      "parameter",
      "key",
      "keys",
      "recognized",
      "example"
    ],
    "type": "object"
  },
  "UnexpectedRelocatedToError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "unexpected_relocated_to",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/UnexpectedRelocatedToErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "UnexpectedRelocatedToErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "attempted_action": {
        "description": "Attempted action.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "required_action": {
        "description": "Required action.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "attempted_action",
      "required_action"
    ],
    "type": "object"
  },
  "UnexpectedSuccessorIdError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "unexpected_successor_id",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/UnexpectedSuccessorIdErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "UnexpectedSuccessorIdErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "attempted_action": {
        "description": "Attempted action.",
        "type": "string"
      },
      "field": {
        "description": "Field.",
        "type": "string"
      },
      "required_action": {
        "description": "Required action.",
        "type": "string"
      }
    },
    "required": [
      "field",
      "attempted_action",
      "required_action"
    ],
    "type": "object"
  },
  "UnknownFilterKeyError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "unknown_filter_key",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/UnknownFilterKeyErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "UnknownFilterKeyErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "example": {
        "description": "Example.",
        "type": "string"
      },
      "key": {
        "description": "Key.",
        "type": "string"
      },
      "valid_keys": {
        "description": "Valid keys.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "key",
      "valid_keys",
      "example"
    ],
    "type": "object"
  },
  "UnknownParameterError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "unknown_parameter",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/UnknownParameterErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "UnknownParameterErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "rejected_params": {
        "description": "Rejected params.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "tool": {
        "description": "Tool.",
        "type": "string"
      },
      "valid_params": {
        "description": "Valid params.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "tool",
      "rejected_params",
      "valid_params"
    ],
    "type": "object"
  },
  "VaultAlreadyExistsError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_already_exists",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultAlreadyExistsErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultAlreadyExistsErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "vault_id": {
        "description": "Vault id.",
        "type": "string"
      }
    },
    "required": [
      "vault_id"
    ],
    "type": "object"
  },
  "VaultConfigValidationErrorError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_config_validation_error",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultConfigValidationErrorErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultConfigValidationErrorErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "errors": {
        "description": "Errors.",
        "items": {
          "type": "string"
        },
        "type": "array"
      }
    },
    "required": [
      "errors"
    ],
    "type": "object"
  },
  "VaultMigrationInFlightError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_migration_in_flight",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultMigrationInFlightErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultMigrationInFlightErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "start_time": {
        "description": "Start time.",
        "type": "string"
      },
      "vault_id": {
        "description": "Vault id.",
        "type": "string"
      }
    },
    "required": [
      "vault_id",
      "start_time"
    ],
    "type": "object"
  },
  "VaultNotFoundError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_not_found",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultNotFoundErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultNotFoundErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "available_vaults": {
        "description": "Available vaults.",
        "items": {
          "type": "string"
        },
        "type": "array"
      },
      "vault_id": {
        "description": "Vault id.",
        "type": "string"
      }
    },
    "required": [
      "vault_id",
      "available_vaults"
    ],
    "type": "object"
  },
  "VaultSourcePathRefusedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_source_path_refused",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultSourcePathRefusedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultSourcePathRefusedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "source_path": {
        "description": "Source path.",
        "type": "string"
      }
    },
    "required": [
      "source_path"
    ],
    "type": "object"
  },
  "VaultSourceStoreRefusedError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_source_store_refused",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultSourceStoreRefusedErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultSourceStoreRefusedErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "operation": {
        "description": "Operation.",
        "type": "string"
      },
      "source_path": {
        "description": "Source path.",
        "type": "string"
      },
      "store_status": {
        "description": "Store status.",
        "type": "integer"
      }
    },
    "required": [
      "source_path",
      "operation"
    ],
    "type": "object"
  },
  "VaultSourceStoreUnavailableError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "vault_source_store_unavailable",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/VaultSourceStoreUnavailableErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "VaultSourceStoreUnavailableErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "operation": {
        "description": "Operation.",
        "type": "string"
      },
      "source_path": {
        "description": "Source path.",
        "type": "string"
      },
      "store_status": {
        "description": "Store status.",
        "type": "integer"
      }
    },
    "required": [
      "source_path",
      "operation"
    ],
    "type": "object"
  },
  "WritePathExistsError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "write_path_exists",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/WritePathExistsErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "WritePathExistsErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "write_to_path": {
        "description": "Write to path.",
        "type": "string"
      }
    },
    "required": [
      "write_to_path"
    ],
    "type": "object"
  },
  "WritePathInvalidError": {
    "additionalProperties": false,
    "description": "The refusal envelope for this error code.",
    "properties": {
      "code": {
        "const": "write_path_invalid",
        "description": "Machine-readable error code selecting this envelope.",
        "type": "string"
      },
      "detail": {
        "$ref": "#/components/schemas/WritePathInvalidErrorDetail"
      },
      "message": {
        "description": "Human-readable error description.",
        "type": "string"
      },
      "read_meta": {
        "$ref": "#/components/schemas/ReadMeta"
      }
    },
    "required": [
      "code",
      "message",
      "detail"
    ],
    "type": "object"
  },
  "WritePathInvalidErrorDetail": {
    "additionalProperties": false,
    "description": "Additional context for this refusal.",
    "properties": {
      "reason": {
        "description": "Reason.",
        "type": "string"
      },
      "write_to_path": {
        "description": "Write to path.",
        "type": "string"
      }
    },
    "required": [
      "write_to_path",
      "reason"
    ],
    "type": "object"
  }
}
"""
)
